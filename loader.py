import os
import importlib
import torch
from omegaconf import OmegaConf
from config import checkpoint_files


models = dict()

def load(name):
	if name not in checkpoint_files:
		raise Exception('no checkpoint file path specified for model "%s"' % name)

	checkpoint_path = checkpoint_files[name]
	config = OmegaConf.load(
		os.path.join(os.path.dirname(__file__), 'configs', '%s.yaml' % name)
	)

	modulename, classname = config.model.target.rsplit('.', 1)
	module = importlib.import_module(modulename)
	cls = getattr(module, classname)
	model = cls(**config.model.get("params", dict()))
	checkpoint = torch.load(checkpoint_path, map_location='cpu')

	model.load_state_dict(checkpoint['state_dict'], strict=False)
	model.half()
	model.cuda()
	model.eval()
	models[name] = model

	return model