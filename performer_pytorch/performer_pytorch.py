import math
import torch
import torch.nn.functional as F
from torch import nn
from einops import rearrange
from functools import partial
from performer_pytorch.reversible import ReversibleSequence, SequentialSequence

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# kernel functions

# transcribed from jax to pytorch from
# https://github.com/google-research/google-research/blob/master/performer/fast_self_attention/fast_self_attention.py

def softmax_kernel(data, *, projection_matrix, is_query, normalize_data=True, eps=1e-4, device = None):
    if normalize_data:
        data_normalizer = 1.0 / (data.shape[-1] ** 0.25)
    else:
        data_normalizer = 1.0

    ratio = 1.0 / (projection_matrix.shape[0] ** 0.5)

    data_mod_shape = data.shape[:(len(data.shape) - 2)] + projection_matrix.shape
    data_thick_random_matrix = torch.zeros(data_mod_shape, device = device) + projection_matrix

    data_dash = torch.einsum('...id,...jd->...ij', (data_normalizer * data), data_thick_random_matrix)

    diag_data = data ** 2
    diag_data = torch.sum(diag_data, dim=-1)
    diag_data = (diag_data / 2.0) * (data_normalizer ** 2)
    diag_data = diag_data.unsqueeze(dim=-1)

    if is_query:
        data_dash = ratio * (
            torch.exp(data_dash - diag_data -
                    torch.max(data_dash, dim=-1, keepdim=True).values) + eps)
    else:
        data_dash = ratio * (
            torch.exp(data_dash - diag_data - torch.max(data_dash)) + eps)

    return data_dash

def orthogonal_matrix_chunk(cols, device = None):
    unstructured_block = torch.randn((cols, cols), device = device)
    q, _ = torch.qr(unstructured_block, some = True)
    return q.t()

def gaussian_orthogonal_random_matrix(nb_rows, nb_columns, scaling = 0, device = None):
    nb_full_blocks = int(nb_rows / nb_columns)

    block_list = []

    for _ in range(nb_full_blocks):
        q = orthogonal_matrix_chunk(nb_columns, device = device)
        block_list.append(q)

    remaining_rows = nb_rows - nb_full_blocks * nb_columns
    if remaining_rows > 0:
        q = orthogonal_matrix_chunk(nb_columns, device = device)
        block_list.append(q[:remaining_rows])

    final_matrix = torch.cat(block_list)

    if scaling == 0:
        multiplier = torch.randn((nb_rows, nb_columns), device = device).norm(dim = 1)
    elif scaling == 1:
        multiplier = math.sqrt((float(nb_columns))) * torch.ones((nb_rows,), device = device)
    else:
        raise ValueError(f'Invalid scaling {scaling}')

    return torch.diag(multiplier) @ final_matrix

# linear attention classes with softmax kernel

def linear_attention(q, k, v):
    context = torch.einsum('...nd,...ne->...de', k, v)
    out = torch.einsum('...de,...nd->...ne', context, q)
    return out

def causal_linear_attention(q, k, v):
    k_cumsum = k.cumsum(dim=-2)
    context = torch.einsum('...nd,...ne->...nde', k, v)
    context = context.cumsum(dim=-3)
    context /= k_cumsum.unsqueeze(dim=-1)
    out = torch.einsum('...nde,...nd->...ne', context, q)
    return out

class FastAttention(nn.Module):
    def __init__(self, dim_heads, nb_features = 256, redraw_projection = True, ortho_scaling = 1, causal = False):
        super().__init__()
        self.causal = causal
        self.dim_heads = dim_heads
        self.nb_features = nb_features
        self.ortho_scaling = ortho_scaling
        self.redraw_projection = redraw_projection

        self.create_projection = partial(gaussian_orthogonal_random_matrix, nb_rows = nb_features, nb_columns = dim_heads, scaling = ortho_scaling)

        if not redraw_projection:
            projection_matrix = self.create_projection()
            self.register_buffer('projection_matrix', projection_matrix)

    def forward(self, q, k, v):
        device = q.device

        if self.redraw_projection:
            projection_matrix = self.create_projection(device = device)
        else:
            projection_matrix = self.projection_matrix

        create_kernel = partial(softmax_kernel, projection_matrix = projection_matrix, device = device)
        q = create_kernel(q, is_query = True)
        k = create_kernel(k, is_query = False)

        attn_fn = linear_attention if not self.causal else causal_linear_attention
        out = attn_fn(q, k, v)
        return out

# classes

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x):
        return self.fn(self.norm(x))

class Chunk(nn.Module):
    def __init__(self, chunks, fn, along_dim = -1):
        super().__init__()
        self.dim = along_dim
        self.chunks = chunks
        self.fn = fn

    def forward(self, x, **kwargs):
        if self.chunks == 1:
            return self.fn(x, **kwargs)
        chunks = x.chunk(self.chunks, dim = self.dim)
        return torch.cat([self.fn(c, **kwargs) for c in chunks], dim = self.dim)

class FeedForward(nn.Module):
    def __init__(self, dim, mult = 4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * mult),
            nn.GELU(),
            nn.Linear(dim * mult, dim)
        )
    def forward(self, x):
        return self.net(x)

class SelfAttention(nn.Module):
    def __init__(self, dim, causal = False, heads = 8, nb_features = 256, redraw_projection = True):
        super().__init__()
        assert dim % heads == 0, 'dimension must be divisible by number of heads'
        self.fast_attention = FastAttention(dim // heads, nb_features, redraw_projection, causal = causal)

        self.heads = heads
        self.to_qkv = nn.Linear(dim, dim * 3, bias = False)
        self.to_out = nn.Linear(dim, dim)

    def forward(self, x):
        b, n, _, h = *x.shape, self.heads
        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b n (qkv h d) -> qkv b h n d', qkv = 3, h = h)

        out = self.fast_attention(q, k, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        out =  self.to_out(out)
        return out

class Performer(nn.Module):
    def __init__(self, dim, depth, heads, causal = False, ff_mult = 4, nb_features = 256, reversible = False, ff_chunks = 1):
        super().__init__()
        layers = nn.ModuleList([])
        for _ in range(depth):
            layers.append(nn.ModuleList([
                PreNorm(dim, SelfAttention(dim, causal = causal, heads = heads, nb_features = nb_features)),
                PreNorm(dim, Chunk(ff_chunks, FeedForward(dim, mult = ff_mult), along_dim = 1))
            ]))
        execute_type = ReversibleSequence if reversible else SequentialSequence
        self.net = execute_type(layers)
    def forward(self, x, **kwargs):
        return self.net(x)

class PerformerLM(nn.Module):
    def __init__(self, *, num_tokens, max_seq_len, dim, depth, heads, causal = False, ff_mult = 4, nb_features = 256, reversible = False, ff_chunks = 1):
        super().__init__()
        self.max_seq_len = max_seq_len
        self.token_emb = nn.Embedding(num_tokens, dim)
        self.pos_emb = nn.Embedding(max_seq_len, dim)
        self.performer = Performer(dim, depth, heads, causal, ff_mult, nb_features, reversible, ff_chunks)
        self.to_logits = nn.Linear(dim, num_tokens)

    def forward(self, x, **kwargs):
        b, n, device = *x.shape, x.device
        x = self.token_emb(x)
        x += self.pos_emb(torch.arange(n, device = device))
        x = self.performer(x)
        return self.to_logits(x)
