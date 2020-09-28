"""
Download datasets from https://people.eecs.berkeley.edu/~taesung_park/CycleGAN/datasets/[DATASET NAME].zip
and extract them into labml_nn/data/cycle_gan/[DATASET NAME]

I've taken pieces of code from https://github.com/eriklindernoren/PyTorch-GAN
"""

import itertools
import random
from pathlib import PurePath, Path

import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from torchvision.utils import make_grid
from torchvision.utils import save_image

from labml import lab, tracker, experiment
from labml_helpers.device import DeviceConfigs
from labml_helpers.module import Module
from labml_helpers.training_loop import TrainingLoopConfigs


class ReplayBuffer:
    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self.data = []

    def push_and_pop(self, data):
        to_return = []
        for element in data:
            if len(self.data) < self.max_size:
                self.data.append(element)
                to_return.append(element)
            else:
                if random.uniform(0, 1) > 0.5:
                    i = random.randint(0, self.max_size - 1)
                    to_return.append(self.data[i].clone())
                    self.data[i] = element
                else:
                    to_return.append(element)
        return torch.stack(to_return)


def load_image(path: str):
    image = Image.open(path)
    if image.mode != 'RGB':
        image = Image.new("RGB", image.size).pase(image)

    return image


class ImageDataset(Dataset):
    def __init__(self, root: PurePath, transforms_, unaligned: bool, mode: str):
        root = Path(root)
        self.transform = transforms.Compose(transforms_)
        self.unaligned = unaligned

        self.files_A = sorted(str(f) for f in (root / f'{mode}A').iterdir())
        self.files_B = sorted(str(f) for f in (root / f'{mode}B').iterdir())

    def __getitem__(self, index):
        return {"a": self.transform(load_image(self.files_A[index % len(self.files_A)])),
                "b": self.transform(load_image(self.files_B[index % len(self.files_B)]))}

    def __len__(self):
        return max(len(self.files_A), len(self.files_B))


def weights_init_normal(m):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        torch.nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif classname.find("BatchNorm2d") != -1:
        torch.nn.init.normal_(m.weight.data, 1.0, 0.02)


class ResidualBlock(Module):
    def __init__(self, in_features: int):
        super().__init__()
        self.block = nn.Sequential(
            nn.ReflectionPad2d(1),
            nn.Conv2d(in_features, in_features, 3),
            nn.InstanceNorm2d(in_features),
            nn.ReLU(inplace=True),
            nn.ReflectionPad2d(1),
            nn.Conv2d(in_features, in_features, 3),
            nn.InstanceNorm2d(in_features),
        )

    def __call__(self, x: torch.Tensor):
        return x + self.block(x)


class GeneratorResNet(Module):
    def __init__(self, input_shape, num_residual_blocks):
        super().__init__()
        channels = input_shape[0]

        # Initial convolution block
        out_features = 64
        layers = [
            nn.ReflectionPad2d(channels),
            nn.Conv2d(channels, out_features, 7),
            nn.InstanceNorm2d(out_features),
            nn.ReLU(inplace=True),
        ]
        in_features = out_features

        # Downsampling
        for _ in range(2):
            out_features *= 2
            layers += [
                nn.Conv2d(in_features, out_features, 3, stride=2, padding=1),
                nn.InstanceNorm2d(out_features),
                nn.ReLU(inplace=True),
            ]
            in_features = out_features

        # Residual blocks
        for _ in range(num_residual_blocks):
            layers += [ResidualBlock(out_features)]

        # Upsampling
        for _ in range(2):
            out_features //= 2
            layers += [
                nn.Upsample(scale_factor=2),
                nn.Conv2d(in_features, out_features, 3, stride=1, padding=1),
                nn.InstanceNorm2d(out_features),
                nn.ReLU(inplace=True),
            ]
            in_features = out_features

        # Output layer
        layers += [nn.ReflectionPad2d(channels), nn.Conv2d(out_features, channels, 7), nn.Tanh()]

        self.layers = nn.Sequential(*layers)

        self.apply(weights_init_normal)

    def __call__(self, x):
        return self.layers(x)


class DiscriminatorBlock(Module):
    def __init__(self, in_filters, out_filters, normalize=True):
        super().__init__()
        layers = [nn.Conv2d(in_filters, out_filters, 4, stride=2, padding=1)]
        if normalize:
            layers.append(nn.InstanceNorm2d(out_filters))
        layers.append(nn.LeakyReLU(0.2, inplace=True))
        self.layers = nn.Sequential(*layers)

    def __call__(self, x: torch.Tensor):
        return self.layers(x)


class Discriminator(Module):
    def __init__(self, input_shape):
        super().__init__()
        channels, height, width = input_shape

        # Calculate output shape of image discriminator (PatchGAN)
        self.output_shape = (1, height // 2 ** 4, width // 2 ** 4)

        self.model = nn.Sequential(
            DiscriminatorBlock(channels, 64, normalize=False),
            DiscriminatorBlock(64, 128),
            DiscriminatorBlock(128, 256),
            DiscriminatorBlock(256, 512),
            nn.ZeroPad2d((1, 0, 1, 0)),
            nn.Conv2d(512, 1, 4, padding=1)
        )

        self.apply(weights_init_normal)

    def forward(self, img):
        return self.model(img)


def sample_images(n, dataset_name, valid_dataloader, generator_ab, generator_ba):
    """Saves a generated sample from the test set"""
    batch = next(iter(valid_dataloader))
    generator_ab.eval()
    generator_ba.eval()
    with torch.no_grad():
        real_a, real_b = batch['a'].to(generator_ab.device), batch['b'].to(generator_ba.device)
        fake_b = generator_ab(real_a)
        fake_a = generator_ba(real_b)

        # Arange images along x-axis
        real_a = make_grid(real_a, nrow=5, normalize=True)
        real_b = make_grid(real_b, nrow=5, normalize=True)
        fake_a = make_grid(fake_a, nrow=5, normalize=True)
        fake_b = make_grid(fake_b, nrow=5, normalize=True)

        # arange images along y-axis
        image_grid = torch.cat((real_a, fake_b, real_b, fake_a), 1)
    save_image(image_grid, "images/%s/%s.png" % (dataset_name, n), normalize=False)


class Configs(TrainingLoopConfigs):
    device: torch.device = DeviceConfigs()
    loop_count: int = 200
    dataset_name: str = 'monet2photo'
    batch_size: int = 1

    data_loader_workers = 8
    is_save_models = True

    learning_rate = 0.0002
    adam_betas = (0.5, 0.999)
    decay_start = 100

    identity_loss = torch.nn.L1Loss()
    cycle_loss = torch.nn.L1Loss()
    gan_loss = torch.nn.MSELoss()

    batch_step = 'cycle_gan_batch_step'

    img_height = 256
    img_width = 256
    img_channels = 3

    n_residual_blocks = 9

    cyclic_loss_coefficient = 10.0
    identity_loss_coefficient = 5.

    sample_interval = 100

    def run(self):
        images_path = lab.get_data_path() / 'cycle_gan' / self.dataset_name

        input_shape = (self.img_channels, self.img_height, self.img_width)
        generator_ab = GeneratorResNet(input_shape, self.n_residual_blocks).to(self.device)
        generator_ba = GeneratorResNet(input_shape, self.n_residual_blocks).to(self.device)
        discriminator_a = Discriminator(input_shape).to(self.device)
        discriminator_b = Discriminator(input_shape).to(self.device)

        generator_optimizer = torch.optim.Adam(
            itertools.chain(generator_ab.parameters(), generator_ba.parameters()),
            lr=self.learning_rate, betas=self.adam_betas)
        discriminator_optimizer = torch.optim.Adam(
            itertools.chain(discriminator_a.parameters(), discriminator_b.parameters()),
            lr=self.learning_rate, betas=self.adam_betas)

        decay_epochs = self.loop_count - self.decay_start
        generator_lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
            generator_optimizer, lr_lambda=lambda e: 1.0 - max(0, e - self.decay_start) / decay_epochs)
        discriminator_lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
            discriminator_optimizer, lr_lambda=lambda e: 1.0 - max(0, e - self.decay_start) / decay_epochs)

        # Image transformations
        transforms_ = [
            transforms.Resize(int(self.img_height * 1.12), Image.BICUBIC),
            transforms.RandomCrop((self.img_height, self.img_width)),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]

        # Training data loader
        dataloader = DataLoader(
            ImageDataset(images_path, transforms_, True, 'train'),
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.data_loader_workers,
        )
        # Test data loader
        valid_dataloader = DataLoader(
            ImageDataset(images_path, transforms_, True, "test"),
            batch_size=5,
            shuffle=True,
            num_workers=self.data_loader_workers,
        )

        # Buffers of previously generated samples
        fake_a_buffer = ReplayBuffer()
        fake_b_buffer = ReplayBuffer()

        for epoch in self.training_loop:
            for i, batch in enumerate(dataloader):
                # Set model input
                real_a, real_b = batch['a'].to(self.device), batch['b'].to(self.device)

                # adversarial ground truths
                valid = torch.ones(real_a.size(0), *discriminator_a.output_shape,
                                   device=self.device, requires_grad=False)
                fake = torch.zeros(real_a.size(0), *discriminator_a.output_shape,
                                   device=self.device, requires_grad=False)

                #  Train generators
                generator_ab.train()
                generator_ba.train()

                # Identity loss
                loss_identity = self.identity_loss(generator_ba(real_a), real_a) + \
                                self.identity_loss(generator_ab(real_b), real_b)

                # GAN loss
                fake_b = generator_ab(real_a)
                fake_a = generator_ba(real_b)

                loss_gan = self.gan_loss(discriminator_b(fake_b), valid) + \
                           self.gan_loss(discriminator_a(fake_a), valid)

                loss_cycle = self.cycle_loss(generator_ba(fake_b), real_a) + \
                             self.cycle_loss(generator_ab(fake_a), real_b)

                # Total loss
                loss_generator = (loss_gan + self.cyclic_loss_coefficient * loss_cycle
                                  + self.identity_loss_coefficient * loss_identity)

                generator_optimizer.zero_grad()
                loss_generator.backward()
                generator_optimizer.step()

                #  Train discriminators
                fake_a_replay = fake_a_buffer.push_and_pop(fake_a)
                fake_b_replay = fake_b_buffer.push_and_pop(fake_b)
                loss_discriminator = self.gan_loss(discriminator_a(real_a), valid) + \
                                     self.gan_loss(discriminator_a(fake_a_replay.detach()), fake) + \
                                     self.gan_loss(discriminator_b(real_b), valid) + \
                                     self.gan_loss(discriminator_b(fake_b_replay.detach()), fake)

                discriminator_optimizer.zero_grad()
                loss_discriminator.backward()
                discriminator_optimizer.step()

                tracker.save({'loss.generator': loss_generator,
                              'loss.discriminator': loss_discriminator,
                              'loss.generator.cycle': loss_cycle,
                              'loss.generator.gan': loss_gan,
                              'loss.generator.identity': loss_identity})

                # If at sample interval save image
                batches_done = epoch * len(dataloader) + i
                if batches_done % self.sample_interval == 0:
                    sample_images(batches_done, self.dataset_name, valid_dataloader, generator_ab, generator_ba)

                tracker.add_global_step(max(len(real_a), len(real_b)))

            # Update learning rates
            generator_lr_scheduler.step()
            discriminator_lr_scheduler.step()


def main():
    conf = Configs()
    experiment.create(name='cycle_gan')
    experiment.configs(conf, 'run')
    with experiment.start():
        conf.run()


if __name__ == '__main__':
    main()
