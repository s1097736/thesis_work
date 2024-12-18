import torch
import pickle
from torch.utils.data import DataLoader

from decorr_mamba.utils.helpers import MambaArgs, TrainingArgs, DefaultArgs, LanguageDatasetMaker
from decorr_mamba.model.decorrelation import DecorrMamba
from decorr_mamba.utils.trainer import MambaTrainer
import os

os.environ["TOKENIZERS_PARALLELISM"] = 'false'

GPU = None

if __name__ == "__main__":

	torch.manual_seed(5)
	torch.autograd.set_detect_anomaly(True)

	# train on song lyrics dataset for now
	print("Loading dataset...")
	with open("../datasets/kaggle_song_lyrics_dataset/kaggle_song_lyrics_dataset.pkl", "rb") as f:
	    seqs = pickle.load(f)
	print("Dataset loaded. ")

	# inner model dimensionalities and batch size
	L = 32
	B = 10
	D = 16
	N = 8

	device = torch.device(f'cuda:{GPU}' if torch.cuda.is_available() else 'cpu')
	print(f"\nTraining with device: {device}")

	mamba_args = MambaArgs(N, D, n_layers=2, vocab_size=1024, device=device)

	print(f"\nCreating model with following args: \n{mamba_args}")

	decorr_model = DecorrMamba("channel_independent", mamba_args, 
		sample_frac=0.1, kappa=0.5, decorr_lr=0.0001).to(device)

	print("Model created.")


	# defining the training protocol
	default_train_args = DefaultArgs().lm_args
	default_train_args["use_lr_sched"] = False

	train_args = TrainingArgs(
	    n_epochs=20, L=L, B=B, lr=1*1.5e-3, **default_train_args, warmup_epochs=0)
	print(f"\nTraining with following training arguments:\n{train_args}")

	datasets = LanguageDatasetMaker(seqs, mamba_args, train_args, total_dataset_frac=0.1,
	                                train_split=0.8, val_split=0.2)

	# creating datasets + trainer
	train_loader = DataLoader(datasets.train_set, B, shuffle=True)
	val_loader   = DataLoader(datasets.val_set, B, shuffle=False)

	trainer = MambaTrainer(mamba_args, train_args, decorr_model)

	trainer.train(train_loader, val_loader, save_checkpoints=False)