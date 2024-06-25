from functools import partial

import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Optional, Tuple, Type

# from timm.models.vision_transformer import Block
from typing import Tuple
from ..utils import get_3d_sincos_pos_embed
from .common import LayerNorm3d, MLPBlock

class ImageEncoderViT(nn.Module):
    def __init__(
        self,
        img_size: int = 224,
        patch_size: int = 16,
        in_chans: int = 3,
        embed_dim: int = 768,
        depth: int = 12,
        num_heads: int = 12,
        mlp_ratio: float = 4.0,
        out_chans: int = 256,
        qkv_bias: bool = True,
        norm_layer = nn.LayerNorm,
        act_layer = nn.GELU,
        use_abs_pos: bool = True,
        use_rel_pos: bool = False,
        rel_pos_zero_init: bool = True,
        window_size: int = 0,
        global_attn_indexes: Tuple[int, ...] = (),
    ):
        super().__init__()

        # --------------------------------------------------------------------------
        # MAE encoder specifics
        self.patch_embed = PatchEmbed(kernel_size=patch_size, stride=patch_size, in_chans=in_chans,
        embed_dim=embed_dim)
        self.embed_dim = embed_dim
        self.patch_size = patch_size
        num_patches = (img_size // patch_size) **3

        self.pos_embed = nn.Parameter(torch.zeros(1, 1024 // patch_size, 1024 // patch_size, embed_dim),requires_grad=False)  # fixed sin-cos embedding
        self.depth_embed = nn.Parameter(torch.zeros(1, img_size // patch_size, embed_dim))
        # print("self.pos_embed shape:", self.pos_embed.shape, "img_size:", img_size)

        self.blocks = nn.ModuleList()
        for i in range(depth):
            block = Block(
                dim=embed_dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                norm_layer=norm_layer,
                act_layer=act_layer,
                use_rel_pos=use_rel_pos,
                rel_pos_zero_init=rel_pos_zero_init,
                window_size=window_size if i not in global_attn_indexes else 0,
                input_size=(img_size // patch_size, img_size // patch_size, img_size // patch_size),
            )
            self.blocks.append(block)
        # self.norm = norm_layer(embed_dim)

        self.in_chans = in_chans
        self.img_size = img_size

        self.neck = nn.Sequential(
            nn.Conv3d(
                embed_dim,
                out_chans,
                kernel_size=1,
                bias=False,
            ),
            LayerNorm3d(out_chans),
            nn.Conv3d(
                out_chans,
                out_chans,
                kernel_size=3,
                padding=1,
                bias=False,
            ),
            LayerNorm3d(out_chans),
        )

    def forward(self, x):
        B, C, H, W, Z = x.shape
        print("x:",x.device)
        
        x = self.patch_embed(x)

        if self.pos_embed is not None:
            pos_embed = F.avg_pool2d(self.pos_embed.permute(0,3,1,2), kernel_size=4)
            pos_embed = F.avg_pool2d(pos_embed,kernel_size=3,stride=1).permute(0,2,3,1).unsqueeze(3) # (1,14,14,1,768)
            pos_embed = pos_embed + (self.depth_embed.unsqueeze(1).unsqueeze(1))
            # print("pos_embed:",x.shape,pos_embed.shape)
            x = x + pos_embed

        # x = x.permute(0,-1,1,2,3).flatten(2).transpose(1,2)
        # print("before block:",x.shape)
        for blk in self.blocks:
            x = blk(x)

        # print("after block:", x.shape)
        b,h,w,z,c = x.shape
        x = x.permute(0,-1,1,2,3)
        x = self.neck(x)
        print("encoder output shape:", x.shape)

        return x
    
class Block(nn.Module):
    """
    Transformer blocks with support of window attention and residual propagation blocks
    Inherited from original SAM, so does Attention, window_partition, and window_unpartition, just changed to 3d form.
    """

    def __init__(
        self,
        dim: int,
        num_heads: int,
        mlp_ratio: float = 4.0,
        qkv_bias: bool = True,
        norm_layer: Type[nn.Module] = nn.LayerNorm,
        act_layer: Type[nn.Module] = nn.GELU,
        use_rel_pos: bool = False,
        rel_pos_zero_init: bool = True,
        window_size: int = 0,
        input_size: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """
        Args:
            dim (int): Number of input channels.
            num_heads (int): Number of attention heads in each ViT block.
            mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
            qkv_bias (bool): If True, add a learnable bias to query, key, value.
            norm_layer (nn.Module): Normalization layer.
            act_layer (nn.Module): Activation layer.
            use_rel_pos (bool): If True, add relative positional embeddings to the attention map.
            rel_pos_zero_init (bool): If True, zero initialize relative positional parameters.
            window_size (int): Window size for window attention blocks. If it equals 0, then
                use global attention.
            input_size (tuple(int, int) or None): Input resolution for calculating the relative
                positional parameter size.
        """
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            use_rel_pos=use_rel_pos,
            rel_pos_zero_init=rel_pos_zero_init,
            input_size=input_size if window_size == 0 else (window_size, window_size, window_size),
        )

        self.norm2 = norm_layer(dim)
        self.mlp = MLPBlock(embedding_dim=dim, mlp_dim=int(dim * mlp_ratio), act=act_layer)

        self.window_size = window_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shortcut = x
        x = self.norm1(x)
        # Window partition
        if self.window_size > 0:
            H, W, Z = x.shape[1], x.shape[2], x.shape[3]
            x, pad_hwz = window_partition(x, self.window_size)

        x = self.attn(x)
        # Reverse window partition
        if self.window_size > 0:
            x = window_unpartition(x, self.window_size, pad_hwz, (H, W, Z))

        x = shortcut + x
        x = x + self.mlp(self.norm2(x))

        return x

class Attention(nn.Module):
    """Multi-head Attention block with relative position embeddings."""

    def __init__(
        self,
        dim: int,
        num_heads: int = 8,
        qkv_bias: bool = True,
        use_rel_pos: bool = False,
        rel_pos_zero_init: bool = True,
        input_size: Optional[Tuple[int, int, int]] = None,
    ) -> None:
        """
        Args:
            dim (int): Number of input channels.
            num_heads (int): Number of attention heads.
            qkv_bias (bool):  If True, add a learnable bias to query, key, value.
            rel_pos (bool): If True, add relative positional embeddings to the attention map.
            rel_pos_zero_init (bool): If True, zero initialize relative positional parameters.
            input_size (tuple(int, int) or None): Input resolution for calculating the relative
                positional parameter size.
        """
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim**-0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.proj = nn.Linear(dim, dim)

        self.use_rel_pos = use_rel_pos
        if self.use_rel_pos:
            assert (
                input_size is not None
            ), "Input size must be provided if using relative positional encoding."
            # initialize relative positional embeddings
            self.rel_pos_h = nn.Parameter(torch.zeros(2 * input_size[0] - 1, head_dim))
            self.rel_pos_w = nn.Parameter(torch.zeros(2 * input_size[1] - 1, head_dim))
            self.rel_pos_z = nn.Parameter(torch.zeros(2 * input_size[2] - 1, head_dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, H, W, Z, _ = x.shape
        # qkv with shape (3, B, nHead, H * W, C)
        qkv = self.qkv(x).reshape(B, H * W * Z, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        # q, k, v with shape (B * nHead, H * W * Z, C)
        q, k, v = qkv.reshape(3, B * self.num_heads, H * W * Z, -1).unbind(0)

        attn = (q * self.scale) @ k.transpose(-2, -1)

        if self.use_rel_pos:
            attn = add_decomposed_rel_pos(attn, q, self.rel_pos_h, self.rel_pos_w, self.rel_pos_z, (H, W, Z), (H, W, Z))

        attn = attn.softmax(dim=-1)
        x = (attn @ v).view(B, self.num_heads, H, W, Z, -1).permute(0, 2, 3, 4, 1, 5).reshape(B, H, W, Z, -1)
        x = self.proj(x)

        return x


def window_partition(x: torch.Tensor, window_size: int) -> Tuple[torch.Tensor, Tuple[int, int, int]]:
    """
    Partition into non-overlapping windows with padding if needed.
    Args:
        x (tensor): input tokens with [B, H, W, C].
        window_size (int): window size.

    Returns:
        windows: windows after partition with [B * num_windows, window_size, window_size, C].
        (Hp, Wp): padded height and width before partition
    """
    B, H, W, Z, C = x.shape

    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size
    pad_z = (window_size - Z % window_size) % window_size

    if pad_h > 0 or pad_w > 0 or pad_z > 0:
        x = F.pad(x, (0, 0, 0, pad_z, 0, pad_w, 0, pad_h))
    Hp, Wp, Zp = H + pad_h, W + pad_w, Z + pad_z

    x = x.view(B, Hp // window_size, window_size, Wp // window_size, window_size, Zp // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 5, 2, 4, 6, 7).contiguous().view(-1, window_size, window_size, window_size, C)
    # print("window size:", windows.shape)
    return windows, (Hp, Wp, Zp)


def window_unpartition(
    windows: torch.Tensor, window_size: int, pad_hwz: Tuple[int, int, int], hwz: Tuple[int, int, int]
) -> torch.Tensor:
    """
    Window unpartition into original sequences and removing padding.
    Args:
        windows (tensor): input tokens with [B * num_windows, window_size, window_size, C].
        window_size (int): window size.
        pad_hw (Tuple): padded height and width (Hp, Wp).
        hw (Tuple): original height and width (H, W) before padding.

    Returns:
        x: unpartitioned sequences with [B, H, W, C].
    """
    Hp, Wp, Zp = pad_hwz
    H, W, Z = hwz
    B = windows.shape[0] // (Hp * Wp * Zp // window_size // window_size // window_size)
    x = windows.view(B, Hp // window_size, Wp // window_size, Zp // window_size, window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 5, 2, 4, 6, 7).contiguous().view(B, Hp, Wp, Zp, -1)

    if Hp > H or Wp > W or Zp > Z:
        x = x[:, :H, :W, :Z, :].contiguous()
    return x


def get_rel_pos(q_size: int, k_size: int, rel_pos: torch.Tensor) -> torch.Tensor:
    """
    Get relative positional embeddings according to the relative positions of
        query and key sizes.
    Args:
        q_size (int): size of query q.
        k_size (int): size of key k.
        rel_pos (Tensor): relative position embeddings (L, C).

    Returns:
        Extracted positional embeddings according to relative positions.
    """
    max_rel_dist = int(2 * max(q_size, k_size) - 1)
    # Interpolate rel pos if needed.
    if rel_pos.shape[0] != max_rel_dist:
        # Interpolate rel pos.
        rel_pos_resized = F.interpolate(
            rel_pos.reshape(1, rel_pos.shape[0], -1).permute(0, 2, 1),
            size=max_rel_dist,
            mode="linear",
        )
        rel_pos_resized = rel_pos_resized.reshape(-1, max_rel_dist).permute(1, 0)
    else:
        rel_pos_resized = rel_pos

    # Scale the coords with short length if shapes for q and k are different.
    q_coords = torch.arange(q_size)[:, None] * max(k_size / q_size, 1.0)
    k_coords = torch.arange(k_size)[None, :] * max(q_size / k_size, 1.0)
    relative_coords = (q_coords - k_coords) + (k_size - 1) * max(q_size / k_size, 1.0)

    return rel_pos_resized[relative_coords.long()]


def add_decomposed_rel_pos(
    attn: torch.Tensor,
    q: torch.Tensor,
    rel_pos_h: torch.Tensor,
    rel_pos_w: torch.Tensor,
    rel_pos_z: torch.Tensor,
    q_size: Tuple[int, int, int],
    k_size: Tuple[int, int, int],
) -> torch.Tensor:
    """
    Calculate decomposed Relative Positional Embeddings from :paper:`mvitv2`.
    https://github.com/facebookresearch/mvit/blob/19786631e330df9f3622e5402b4a419a263a2c80/mvit/models/attention.py   # noqa B950
    Args:
        attn (Tensor): attention map.
        q (Tensor): query q in the attention layer with shape (B, q_h * q_w, C).
        rel_pos_h (Tensor): relative position embeddings (Lh, C) for height axis.
        rel_pos_w (Tensor): relative position embeddings (Lw, C) for width axis.
        q_size (Tuple): spatial sequence size of query q with (q_h, q_w).
        k_size (Tuple): spatial sequence size of key k with (k_h, k_w).

    Returns:
        attn (Tensor): attention map with added relative positional embeddings.
    """
    q_h, q_w, q_z = q_size
    k_h, k_w, k_z = k_size
    Rh = get_rel_pos(q_h, k_h, rel_pos_h)
    Rw = get_rel_pos(q_w, k_w, rel_pos_w)
    Rz = get_rel_pos(q_z, k_z, rel_pos_z)

    B, _, dim = q.shape
    r_q = q.reshape(B, q_h, q_w, q_z, dim)
    rel_h = torch.einsum("bhwzc,hkc->bhwzk", r_q, Rh)
    rel_w = torch.einsum("bhwzc,wkc->bhwzk", r_q, Rw)
    rel_z = torch.einsum("bhwzc,zkc->bhwzk", r_q, Rz)
    # print("rel_h,w,z shape:", rel_h.shape, rel_w.shape, rel_z.shape)

    attn = (
        attn.view(B, q_h, q_w, q_z, k_h, k_w, k_z) + rel_h[:, :, :, :, :, None, None]  + rel_w[:, :, :, :, None, :, None] + rel_z[:, :, :, :, None, None, :]
    ).view(B, q_h * q_w * q_z, k_h * k_w * k_z)

    return attn
    
class PatchEmbed(nn.Module):
    """
    Image to Patch Embedding.
    """

    def __init__(
        self,
        kernel_size: Tuple[int, int] = (16, 16),
        stride: Tuple[int, int] = (16, 16),
        padding: Tuple[int, int] = (0, 0),
        in_chans: int = 3,
        embed_dim: int = 768,
    ) -> None:
        """
        Args:
            kernel_size (Tuple): kernel size of the projection layer.
            stride (Tuple): stride of the projection layer.
            padding (Tuple): padding size of the projection layer.
            in_chans (int): Number of input image channels.
            embed_dim (int): Patch embedding dimension.
        """
        super().__init__()

        self.proj = nn.Conv2d(
            in_chans, embed_dim, kernel_size=kernel_size, stride=stride, padding=padding
        )
        self.embed_dim = embed_dim
        self.patch_size = kernel_size
        # self.attn1 = Attention(dim=embed_dim,num_heads=8)
        # self.attn2 = Attention(dim=embed_dim,num_heads=2)

        self.lin = nn.Linear(kernel_size*3, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B,C,H,W,Z = x.shape

        xy = torch.zeros(B,self.embed_dim,H // self.patch_size,W // self.patch_size,Z).to(x.device)
        xz = torch.zeros(B,self.embed_dim,H // self.patch_size,W,Z // self.patch_size).to(x.device)
        yz = torch.zeros(B,self.embed_dim,H,W // self.patch_size,Z // self.patch_size).to(x.device)
        for j in range(Z):
            # print(xy[:,:,:,:,j].shape, self.patch_embed(x[:,:,:,:,j]).shape)
            xy[:,:,:,:,j] = self.proj(x[:,:,:,:,j])
            xz[:,:,:,j,:] = self.proj(x[:,:,:,j,:])
            yz[:,:,j,:,:] = self.proj(x[:,:,j,:,:])
        xy = xy.reshape(B,self.embed_dim,H // self.patch_size,W // self.patch_size,Z // self.patch_size,self.patch_size)
        xz = xz.permute(0,1,2,4,3).reshape(B,self.embed_dim,H // self.patch_size, W // self.patch_size, self.patch_size, Z // self.patch_size).permute(0,1,2,3,5,4)
        yz = yz.permute(0,1,3,4,2).reshape(B,self.embed_dim,H // self.patch_size, self.patch_size, W // self.patch_size, Z // self.patch_size).permute(0,1,2,4,5,3) # (B,C,H,W,Z,patchsize)

        # x = torch.cat((xy,xz,yz),dim=-1).to(x.device)
        # print("before attention:",x.shape)
        # xy = xy.permute(0,-1,2,3,4,1).reshape(B*self.patch_size,H // self.patch_size,W // self.patch_size,Z // self.patch_size,self.embed_dim)
        # xy = self.attn1(xy)
        # yz = yz.permute(0,-1,2,3,4,1).reshape(B*self.patch_size,H // self.patch_size,W // self.patch_size,Z // self.patch_size,self.embed_dim)
        # yz = self.attn1(yz)
        # xz = xz.permute(0,-1,2,3,4,1).reshape(B*self.patch_size,H // self.patch_size,W // self.patch_size,Z // self.patch_size,self.embed_dim)
        # xz = self.attn1(xz)
        # print("after attention:",xy.shape)
        x = torch.cat((yz,xz,xy),dim=-1).to(x.device)
        x = self.lin(x).squeeze(-1)
        # print("s",x.shape)
        x = x.permute(0,2,3,4,1)
        # x = x.flatten(2).transpose(1, 2)
        # print("patch embed x:", x.shape, x.dtype, "x grad_fn:", x.grad_fn)
        return x