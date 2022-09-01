import argparse, os, sys, glob
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
from random import randint
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from tqdm import tqdm, trange
# from imwatermark import WatermarkEncoder
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
import time
from pytorch_lightning import seed_everything, _logger
import logging
from torch import autocast
from contextlib import contextmanager, nullcontext

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor


# # load safety model
# safety_model_id = "CompVis/stable-diffusion-safety-checker"
# safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
# safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)

# Don't show info messages (about the seed) from pytorch_lightning
_logger.setLevel(logging.WARNING)

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    images = (images * 255).round().astype("uint8")
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model


# def put_watermark(img, wm_encoder=None):
#     if wm_encoder is not None:
#         img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
#         img = wm_encoder.encode(img, 'dwtDct')
#         img = Image.fromarray(img[:, :, ::-1])
#     return img


# def load_replacement(x):
#     try:
#         return x
#         hwc = x.shape
#         y = Image.open("assets/rick.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
#         y = (np.array(y)/255.0).astype(x.dtype)
#         assert y.shape == x.shape
#         return y
#     except Exception:
#         return x


# def check_safety(x_image):
#     safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
#     x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
#     assert x_checked_image.shape[0] == len(has_nsfw_concept)
#     for i in range(len(has_nsfw_concept)):
#         if has_nsfw_concept[i]:
#             x_checked_image[i] = load_replacement(x_checked_image[i])
#     return x_checked_image, has_nsfw_concept


def input_prompt(opt, repeatParser):
    result = None
    prompts = input("\n\nprompt:")
    (repeatOpt, extraArgs) = repeatParser.parse_known_args(prompts.split())
    if repeatOpt.prompt is not None:
        result = " ".join(repeatOpt.prompt)
        if result == "":
            result = None
    if repeatOpt.plms:
        opt.plms = True
    elif repeatOpt.ddim:
        opt.plms = False
    if repeatOpt.skip_grid:
        opt.skip_grid = True
    elif repeatOpt.save_grid:
        opt.skip_grid = False
    if repeatOpt.iter is not None:
        opt.n_iter = repeatOpt.iter
    if repeatOpt.samples is not None:
        opt.n_samples = repeatOpt.samples
    if repeatOpt.rows is not None:
        opt.n_rows = repeatOpt.rows
    if repeatOpt.steps is not None:
        opt.ddim_steps = repeatOpt.steps
    if repeatOpt.scale is not None:
        opt.scale = repeatOpt.scale
    if repeatOpt.seed is not None:
        opt.seed = repeatOpt.seed
        seed_everything(opt.seed)
        tqdm.write("Seed set to: " + str(opt.seed))
    if repeatOpt.W is not None:
        if opt.W % 64 == 0:
            opt.W = repeatOpt.W
        else:
            print("--W must be a multiple of 64")
            result = None
    if repeatOpt.H is not None:
        if opt.H % 64 == 0:
            opt.H = repeatOpt.H
        else:
            print("--H must be a multiple of 64")
            result = None
    if len(extraArgs) > 0:
        print("Unknown arguments: " + ":".join(extraArgs) + "\n")
        result = None
    if result is None:
        repeatParser.print_help()
    else:
        print(" ")
        
    return result


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/txt2img-samples"
    )
    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save individual samples. For speed measurements.",
    )
    parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )
    parser.add_argument(
        "--laion400m",
        action='store_true',
        help="uses the LAION400M model",
    )
    parser.add_argument(
        "--fixed_code",
        action='store_true',
        help="if enabled, uses the same starting code across samples ",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=1,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=1,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=2,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/model.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )
    opt = parser.parse_args()

    repeatParser = argparse.ArgumentParser(prog="", add_help=False)    
    repeatParser.add_argument(
        "prompt",
        type=str,
        nargs="*",
        help="the prompt to render"
    )
    repeatParser.add_argument(
        "--steps",
        type=int,
        help="number of ddim sampling steps",
    )
    repeatParser.add_argument(
        "--samples",
        type=int,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
    )
    repeatParser.add_argument(
        "--iter",
        type=int,
        help="sample this often (iterations)",
    )
    repeatParser.add_argument(
        "--rows",
        type=int,
        help="rows (actually columns) in the grid (set to 0 to get the default: n_samples)",
    )
    repeatParser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )
    repeatParser.add_argument(
        "--save_grid",
        action='store_true',
        help="save a grid, if samples > 0",
    )
    repeatParser.add_argument(
        "--H",
        type=int,
        help="image height, in pixel space",
    )
    repeatParser.add_argument(
        "--W",
        type=int,
        help="image width, in pixel space",
    )
    repeatParser.add_argument(
        "--scale",
        type=float,
        help="unconditional guidance scale: 0.0 to 15.0",
    )
    repeatParser.add_argument(
        "--seed",
        type=int,
        help="the seed (for reproducible sampling)",
    )
    repeatParser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
    )
    repeatParser.add_argument(
        "--ddim",
        action='store_true',
        help="use ddim sampling",
    )

    opt.plms = True

    if opt.laion400m:
        print("Falling back to LAION 400M model...")
        opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
        opt.ckpt = "models/ldm/text2img-large/model.ckpt"
        opt.outdir = "outputs/txt2img-samples-laion400m"

    seed_everything(opt.seed)
    tqdm.write("Seed set to: " + str(opt.seed))

    config = OmegaConf.load(f"{opt.config}")
    model = load_model_from_config(config, f"{opt.ckpt}")

    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)

    plmsSampler = PLMSSampler(model)
    ddimSampler = DDIMSampler(model)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    # print("Creating invisible watermark encoder (see https://github.com/ShieldMnt/invisible-watermark)...")
    # wm = "StableDiffusionV1"
    # wm_encoder = None
    # wm_encoder.set_watermark('bytes', wm.encode('utf-8'))

    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    precision_scope = autocast if opt.precision=="autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                while True:
                    prompts = None
                    while prompts is None:
                        prompts = input_prompt(opt, repeatParser)

                    start_code = None
                    if opt.fixed_code:
                        start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)

                    tic = time.time()
                    all_samples = list()

                    batch_size = opt.n_samples
                    assert prompts is not None
                    data = [batch_size * [prompts]]

                    for n in trange(opt.n_iter, desc="Sampling"):
                        for prompts in tqdm(data, desc="data"):
                            uc = None
                            if opt.scale != 1.0:
                                uc = model.get_learned_conditioning(batch_size * [""])
                            c = model.get_learned_conditioning(prompts)
                            shape = [opt.C, opt.H // opt.f, opt.W // opt.f]

                            if opt.plms:
                                samples_ddim, _ = plmsSampler.sample(S=opt.ddim_steps,
                                                                conditioning=c,
                                                                batch_size=opt.n_samples,
                                                                shape=shape,
                                                                verbose=False,
                                                                unconditional_guidance_scale=opt.scale,
                                                                unconditional_conditioning=uc,
                                                                eta=opt.ddim_eta,
                                                                x_T=start_code)
                            else:
                                samples_ddim, _ = ddimSampler.sample(S=opt.ddim_steps,
                                                                conditioning=c,
                                                                batch_size=opt.n_samples,
                                                                shape=shape,
                                                                verbose=False,
                                                                unconditional_guidance_scale=opt.scale,
                                                                unconditional_conditioning=uc,
                                                                eta=opt.ddim_eta,
                                                                x_T=start_code)

                            x_samples_ddim = model.decode_first_stage(samples_ddim)
                            x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                            x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()

                            x_checked_image = x_samples_ddim

                            x_checked_image_torch = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                            if not opt.skip_save:
                                for idx, x_sample in enumerate(x_checked_image_torch):
                                    x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                    img = Image.fromarray(x_sample.astype(np.uint8))
                                    # img = put_watermark(img, wm_encoder)

                                    while os.path.exists(os.path.join(sample_path, f"{base_count:05}.png")):
                                        base_count += 1

                                    batchInfo = ""
                                    if opt.n_samples > 0:
                                        batchInfo = ", batched: " + str(idx + 1) + " of " + str(len(x_checked_image_torch))
                                    metadata = PngInfo()
                                    metadata.add_text("Author", "Stable Diffusion Checkpoint v1.4")
                                    metadata.add_text("Description", str(prompts[idx]))
                                    metadata.add_text("Comment",
                                        "seed: " + str(opt.seed) +
                                        ", steps: " + str(opt.ddim_steps) +
                                        ", scale: " + str(opt.scale) +
                                        ", plms: " + str(opt.plms) +
                                        batchInfo)

                                    img.save(os.path.join(sample_path, f"{base_count:05}.png"), pnginfo=metadata)
                                    base_count += 1

                            if not opt.skip_grid and opt.n_samples > 1:
                                all_samples.append(x_checked_image_torch)

                        newSeed = randint(0, 999999)
                        seed_everything(newSeed)
                        opt.seed = newSeed
                        tqdm.write("Seed set to: " + str(opt.seed))

                    if not opt.skip_grid and opt.n_samples > 1:
                        # additionally, save as grid
                        grid = torch.stack(all_samples, 0)
                        grid = rearrange(grid, 'n b c h w -> (n b) c h w')
                        grid = make_grid(grid, nrow=n_rows)

                        # to image
                        grid = 255. * rearrange(grid, 'c h w -> h w c').cpu().numpy()
                        img = Image.fromarray(grid.astype(np.uint8))
                        # img = put_watermark(img, wm_encoder)

                        while os.path.exists(os.path.join(outpath, f'grid-{grid_count:04}.png')):
                            grid_count += 1

                        metadata = PngInfo()
                        metadata.add_text("Author", "Stable Diffusion Checkpoint v1.4")
                        metadata.add_text("Description", str(prompts[0]))
                        metadata.add_text("Comment",
                            "steps: " + str(opt.ddim_steps) +
                            ", scale: " + str(opt.scale) +
                            ", plms: " + str(opt.plms))

                        img.save(os.path.join(outpath, f'grid-{grid_count:04}.png'), pnginfo=metadata)
                        grid_count += 1

                    toc = time.time()

    print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
          f" \nEnjoy.")


if __name__ == "__main__":
    main()
