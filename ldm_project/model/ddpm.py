# minimal DDPM implementation

import torch
import torch.nn as nn
from torch import Tensor

from typing import Tuple
import itertools

from tunet import TimeUnet as Unet


def beta_scheduling(timesteps: int) -> Tensor:
    scale = 1000 / timesteps
    beta_start = scale * 0.0001
    beta_end = scale * 0.02
    return torch.linspace(beta_start, beta_end, timesteps, dtype=torch.float32)


class Diffusion(nn.Module):
    def __init__(self, device):
        super().__init__()

        self.network = Unet(
            in_chans=1,
            out_chans=1,
            chans=64,
            num_pool_layers=3,
            time_emb_dim=256,
        )
        self.timesteps: int = 1000
        # Register as buffers so they move with the model
        self.register_buffer('betas', beta_scheduling(timesteps=self.timesteps))
        self.register_buffer('alphas', 1.0 - self.betas)
        self.register_buffer('a_cumprod', torch.cumprod(self.alphas, dim=0))
        self.register_buffer('sqrt_a_cumprod', torch.sqrt(self.a_cumprod))
        self.register_buffer('sqrt_one_minus_a_cumprod', torch.sqrt(1 - self.a_cumprod))

    def forward(self, lab: Tensor) -> tuple[Tensor, Tensor]:
        """
        Forward pass of the diffusion model.
        Args:
            lab (Tensor): Input tensor of shape (B, C, H, W) where B is batch size,
                          C is number of channels, H is height, and W is width.
        Returns:
            tuple[Tensor, Tensor]: A tuple containing:
                - output (Tensor): The predicted noise from the network.
                - target (Tensor): The actual noise added to the input tensor.
        Raises:
            AssertionError: If the input tensor is not 4D.
        """
        device = lab.device
        if not (lab.dim() == 4):
            raise AssertionError("Tensors must be 4D")
        batch_size = lab.shape[0]

        t = torch.randint(
            low=0,
            high=self.timesteps,
            size=(batch_size,),
            device=device,
            dtype=torch.long,
        )  # sample timesteps
        noise = torch.randn_like(lab).to(device)  # sample noise

        x_t = (
            self.sqrt_a_cumprod.gather(0, t).view(batch_size, 1, 1, 1) * lab
            + self.sqrt_one_minus_a_cumprod.gather(0, t).view(batch_size, 1, 1, 1) * noise
        ).type(torch.float32)

        target = noise
        output = self.network(x_t, t.type(torch.float32))
        return (output, target)

    @torch.inference_mode()
    def recon(self, out_size: torch.Size, interval: float = 50):
        device = next(self.parameters()).device

        batch_size = out_size[0]

        times = torch.linspace(
            0, self.timesteps - 1, int(self.timesteps / interval), dtype=torch.long
        )
        times = list(reversed(times.int().tolist()))
        time_pairs = list(itertools.pairwise(times))

        noise = torch.randn(out_size).to(device)
        x_t = noise.type(torch.float32)

        for time, time_next in time_pairs:
            t_batch = torch.full(
                (batch_size,), time, device=device, dtype=torch.long
            )
            t_next_batch = torch.full(
                (batch_size,), time_next, device=device, dtype=torch.long
            )

            # network inference
            predicted_noise = self.network(x_t, t_batch.type(torch.float32))

            if interval == 1:
                a_cumprod_t = self.a_cumprod.gather(0, t_batch).view(batch_size, 1, 1, 1)
                a_cumprod_t_next = self.a_cumprod.gather(0, t_next_batch).view(
                    batch_size, 1, 1, 1
                )
                alpha_t = self.alphas.gather(0, t_batch).view(batch_size, 1, 1, 1)
                noise = torch.randn_like(x_t)

                mu = (
                    x_t - (1 - alpha_t) / torch.sqrt(1 - a_cumprod_t) * predicted_noise
                ) / torch.sqrt(
                    alpha_t
                )  # predicted mean
                sigma = torch.sqrt(
                    (1 - alpha_t) * ((1 - a_cumprod_t_next) / (1 - a_cumprod_t))
                )
                x_next = mu + sigma * noise
                x_t = x_next
            else:
                # DDIM sampling (works for arbitrary step sizes)
                a_cumprod_t = self.a_cumprod.gather(0, t_batch).view(batch_size, 1, 1, 1)
                a_cumprod_t_next = self.a_cumprod.gather(0, t_next_batch).view(
                    batch_size, 1, 1, 1
                )
                
                # Predict x_0 from x_t and predicted noise
                pred_x0 = (x_t - torch.sqrt(1 - a_cumprod_t) * predicted_noise) / torch.sqrt(a_cumprod_t)
                
                # Compute x_{t-interval} using DDIM formula
                x_next = (
                    torch.sqrt(a_cumprod_t_next) * pred_x0 + 
                    torch.sqrt(1 - a_cumprod_t_next) * predicted_noise
                )
                x_t = x_next

        return x_t.type(torch.float32)
