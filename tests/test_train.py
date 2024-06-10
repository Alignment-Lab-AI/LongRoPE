import pytest
import torch
from torch.utils.data import DataLoader
from transformers import GPT2Tokenizer
from train import CustomDataset, collate_fn, preprocess_data, validate_targets, train
from src.main import LongRoPEModel


def test_custom_dataset():
    sequences = [[1, 2, 3], [4, 5, 6]]
    targets = [[2, 3, 4], [5, 6, 7]]
    dataset = CustomDataset(sequences, targets)
    assert len(dataset) == 2
    assert dataset[0] == (sequences[0], targets[0])


def test_collate_fn():
    batch = [([1, 2, 3], [2, 3, 4]), ([4, 5], [5, 6])]
    inputs, targets = collate_fn(batch)
    assert inputs.shape == (2, 3)
    assert targets.shape == (2, 3)
