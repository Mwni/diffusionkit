import torch
import importlib
import numpy as np
import multiprocessing as mp
from collections import abc
from threading import Thread
from queue import Queue
from inspect import isfunction
from PIL import Image, ImageDraw, ImageFont
from scipy import integrate


def latent_to_images(latent, model):
	x_samples = model.decode_first_stage(latent)
	images = []

	for i in range(len(x_samples)):
		x_sample = x_samples[i]
		x_sample = torch.clamp((x_sample + 1.0) / 2.0, min=0.0, max=1.0)
		x_sample = x_sample.cpu().numpy()
		x_sample = 255. * np.transpose(x_sample, (1, 2, 0))
		x_sample = x_sample.astype(np.uint8)
		
		image = Image.fromarray(x_sample)
		images.append(image)
	
	return images


def to_d(x, sigma, denoised):
	'''Converts a denoiser output to a Karras ODE derivative.'''
	return (x - denoised) / append_dims(sigma, x.ndim)

def linear_multistep_coeff(order, t, i, j):
    if order - 1 > i:
        raise ValueError(f'Order {order} too high for step {i}')
    def fn(tau):
        prod = 1.
        for k in range(order):
            if j == k:
                continue
            prod *= (tau - t[i - k]) / (t[i - j] - t[i - k])
        return prod
    return integrate.quad(fn, t[i], t[i + 1], epsrel=1e-4)[0]

def get_ancestral_step(sigma_from, sigma_to, eta=1.):
    """Calculates the noise level (sigma_down) to step down to and the amount
    of noise to add (sigma_up) when doing an ancestral sampling step."""
    if not eta:
        return sigma_to, 0.
    sigma_up = min(sigma_to, eta * (sigma_to ** 2 * (sigma_from ** 2 - sigma_to ** 2) / sigma_from ** 2) ** 0.5)
    sigma_down = (sigma_to ** 2 - sigma_up ** 2) ** 0.5
    return sigma_down, sigma_up


def append_zero(x):
	return torch.cat([x, x.new_zeros([1])])

def append_dims(x, target_dims):
	"""Appends dimensions to the end of a tensor until it has target_dims dimensions."""
	dims_to_append = target_dims - x.ndim
	if dims_to_append < 0:
		raise ValueError(f'input has {x.ndim} dims but target_dims is {target_dims}, which is less')
	return x[(...,) + (None,) * dims_to_append]

def create_random_tensors(shape, seeds):
	xs = []

	for seed in seeds:
		torch.manual_seed(seed)
		xs.append(torch.randn(shape, device='cpu'))

	return torch.stack(xs)

def resize_image(im, width, height, mode='stretch'):
	if im.width == width and im.height == height:
		return im

	LANCZOS = (Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)

	if mode == 'stretch':
		res = im.resize((width, height), resample=LANCZOS)
	elif mode == 'pad':
		ratio = width / height
		src_ratio = im.width / im.height

		src_w = width if ratio > src_ratio else im.width * height // im.height
		src_h = height if ratio <= src_ratio else im.height * width // im.width

		resized = im.resize((src_w, src_h), resample=LANCZOS)
		res = Image.new("RGBA", (width, height))
		res.paste(resized, box=(width // 2 - src_w // 2, height // 2 - src_h // 2))
	elif mode == 'repeat':
		ratio = width / height
		src_ratio = im.width / im.height

		src_w = width if ratio < src_ratio else im.width * height // im.height
		src_h = height if ratio >= src_ratio else im.height * width // im.width

		resized = im.resize((src_w, src_h), resample=LANCZOS)
		res = Image.new("RGBA", (width, height))
		res.paste(resized, box=(width // 2 - src_w // 2, height // 2 - src_h // 2))

		if ratio < src_ratio:
			fill_height = height // 2 - src_h // 2
			res.paste(resized.resize((width, fill_height), box=(0, 0, width, 0)), box=(0, 0))
			res.paste(resized.resize((width, fill_height), box=(0, resized.height, width, resized.height)), box=(0, fill_height + src_h))
		elif ratio > src_ratio:
			fill_width = width // 2 - src_w // 2
			res.paste(resized.resize((fill_width, height), box=(0, 0, 0, height)), box=(0, 0))
			res.paste(resized.resize((fill_width, height), box=(resized.width, 0, resized.width, height)), box=(fill_width + src_w, 0))

	return res


def log_txt_as_img(wh, xc, size=10):
	# wh a tuple of (width, height)
	# xc a list of captions to plot
	b = len(xc)
	txts = list()
	for bi in range(b):
		txt = Image.new("RGB", wh, color="white")
		draw = ImageDraw.Draw(txt)
		font = ImageFont.truetype('data/DejaVuSans.ttf', size=size)
		nc = int(40 * (wh[0] / 256))
		lines = "\n".join(xc[bi][start:start + nc] for start in range(0, len(xc[bi]), nc))

		try:
			draw.text((0, 0), lines, fill="black", font=font)
		except UnicodeEncodeError:
			print("Cant encode string for logging. Skipping.")

		txt = np.array(txt).transpose(2, 0, 1) / 127.5 - 1.0
		txts.append(txt)
	txts = np.stack(txts)
	txts = torch.tensor(txts)
	return txts


def ismap(x):
	if not isinstance(x, torch.Tensor):
		return False
	return (len(x.shape) == 4) and (x.shape[1] > 3)


def isimage(x):
	if not isinstance(x, torch.Tensor):
		return False
	return (len(x.shape) == 4) and (x.shape[1] == 3 or x.shape[1] == 1)


def exists(x):
	return x is not None


def default(val, d):
	if exists(val):
		return val
	return d() if isfunction(d) else d


def mean_flat(tensor):
	"""
	https://github.com/openai/guided-diffusion/blob/27c20a8fab9cb472df5d6bdd6c8d11c8f430b924/guided_diffusion/nn.py#L86
	Take the mean over all non-batch dimensions.
	"""
	return tensor.mean(dim=list(range(1, len(tensor.shape))))


def count_params(model, verbose=False):
	total_params = sum(p.numel() for p in model.parameters())
	if verbose:
		print(f"{model.__class__.__name__} has {total_params * 1.e-6:.2f} M params.")
	return total_params


def instantiate_from_config(config):
	if not "target" in config:
		if config == '__is_first_stage__':
			return None
		elif config == "__is_unconditional__":
			return None
		raise KeyError("Expected key `target` to instantiate.")
	return get_obj_from_str(config["target"])(**config.get("params", dict()))


def get_obj_from_str(string, reload=False):
	module, cls = string.rsplit(".", 1)
	if reload:
		module_imp = importlib.import_module(module)
		importlib.reload(module_imp)
	return getattr(importlib.import_module(module, package=None), cls)


def _do_parallel_data_prefetch(func, Q, data, idx, idx_to_fn=False):
	# create dummy dataset instance

	# run prefetching
	if idx_to_fn:
		res = func(data, worker_id=idx)
	else:
		res = func(data)
	Q.put([idx, res])
	Q.put("Done")


def parallel_data_prefetch(
		func: callable, data, n_proc, target_data_type="ndarray", cpu_intensive=True, use_worker_id=False
):
	# if target_data_type not in ["ndarray", "list"]:
	#     raise ValueError(
	#         "Data, which is passed to parallel_data_prefetch has to be either of type list or ndarray."
	#     )
	if isinstance(data, np.ndarray) and target_data_type == "list":
		raise ValueError("list expected but function got ndarray.")
	elif isinstance(data, abc.Iterable):
		if isinstance(data, dict):
			print(
				f'WARNING:"data" argument passed to parallel_data_prefetch is a dict: Using only its values and disregarding keys.'
			)
			data = list(data.values())
		if target_data_type == "ndarray":
			data = np.asarray(data)
		else:
			data = list(data)
	else:
		raise TypeError(
			f"The data, that shall be processed parallel has to be either an np.ndarray or an Iterable, but is actually {type(data)}."
		)

	if cpu_intensive:
		Q = mp.Queue(1000)
		proc = mp.Process
	else:
		Q = Queue(1000)
		proc = Thread
	# spawn processes
	if target_data_type == "ndarray":
		arguments = [
			[func, Q, part, i, use_worker_id]
			for i, part in enumerate(np.array_split(data, n_proc))
		]
	else:
		step = (
			int(len(data) / n_proc + 1)
			if len(data) % n_proc != 0
			else int(len(data) / n_proc)
		)
		arguments = [
			[func, Q, part, i, use_worker_id]
			for i, part in enumerate(
				[data[i: i + step] for i in range(0, len(data), step)]
			)
		]
	processes = []
	for i in range(n_proc):
		p = proc(target=_do_parallel_data_prefetch, args=arguments[i])
		processes += [p]

	# start processes
	print(f"Start prefetching...")
	import time

	start = time.time()
	gather_res = [[] for _ in range(n_proc)]
	try:
		for p in processes:
			p.start()

		k = 0
		while k < n_proc:
			# get result
			res = Q.get()
			if res == "Done":
				k += 1
			else:
				gather_res[res[0]] = res[1]

	except Exception as e:
		print("Exception: ", e)
		for p in processes:
			p.terminate()

		raise e
	finally:
		for p in processes:
			p.join()
		print(f"Prefetching complete. [{time.time() - start} sec.]")

	if target_data_type == 'ndarray':
		if not isinstance(gather_res[0], np.ndarray):
			return np.concatenate([np.asarray(r) for r in gather_res], axis=0)

		# order outputs
		return np.concatenate(gather_res, axis=0)
	elif target_data_type == 'list':
		out = []
		for r in gather_res:
			out.extend(r)
		return out
	else:
		return gather_res