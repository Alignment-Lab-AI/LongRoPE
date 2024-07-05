# Train a LongRoPE model on a given dataset
# %%
from src.main import LongRoPEModel
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from torch.nn.utils.rnn import pad_sequence
from torch.cuda.amp import autocast, GradScaler
from torch.optim.lr_scheduler import CosineAnnealingLR
import gzip
from transformers import GPT2Tokenizer
from datasets import load_dataset
from importlib import reload
import src.main
from accelerate import Accelerator
import wandb
import os

reload(src.main)

# Initialize the accelerator
accelerator = Accelerator()


# %%
class CustomDataset(Dataset):
    """Custom dataset for handling sequences and targets."""

    def __init__(self, sequences, targets):
        self.sequences = sequences
        self.targets = targets

    def __len__(self):
        return len(self.sequences)

    def __getitem__(self, idx):
        return self.sequences[idx], self.targets[idx]


def load_data(filename):
    """Load data from a gzip file."""
    with gzip.open(filename, "rt", encoding="utf-8") as f:
        data = f.read()
    return data


def collate_fn(batch):
    """Custom collate function to pad data batches."""
    if not batch:
        return torch.tensor([]), torch.tensor([])
    inputs, targets = zip(*batch)
    padded_inputs = pad_sequence(
        [torch.tensor(seq) for seq in inputs], batch_first=True, padding_value=0
    )
    padded_targets = pad_sequence(
        [torch.tensor(tgt) for tgt in targets], batch_first=True, padding_value=-1
    )
    return padded_inputs, padded_targets


def create_sliding_window_chunks(tokenized_data, max_length=65536, overlap=4096):
    """Create sliding window chunks from tokenized data."""
    sequences = []
    start = 0
    while start < len(tokenized_data):
        end = start + max_length
        if end >= len(tokenized_data):
            # If the remaining sequence is shorter than max_length, append it as is
            sequences.append(tokenized_data[start:])
        else:
            # Split the sequence into chunks of max_length with overlap
            while start < end:
                chunk_end = min(start + max_length, end)
                sequences.append(tokenized_data[start:chunk_end])
                start += max_length - overlap
    return sequences


def validate_targets(targets, vocab_size):
    """Validate that all target indices are within the vocabulary size."""
    for target_batch in targets:
        if any(t >= vocab_size for t in target_batch):
            raise ValueError("Target index out of vocabulary size range.")
    return True


def preprocess_data(data, tokenizer, max_length, overlap):
    """
    Preprocess the input data by tokenizing it in chunks and creating sliding window sequences.

    Args:
        data (str): Input data as a string.
        tokenizer: Tokenizer object for encoding the data.
        max_length (int): Maximum sequence length for each chunk.
        overlap (int): Overlap size between consecutive chunks.

    Returns:
        list: List of preprocessed sequences.
    """
    sequences = []
    start = 0
    while start < len(data):
        end = start + max_length
        chunk = data[start:end]
        tokenized_chunk = tokenizer.encode(chunk)

        # Create sliding window sequences from the tokenized chunk
        chunk_sequences = create_sliding_window_chunks(
            tokenized_chunk, max_length=max_length, overlap=overlap
        )
        sequences.extend(chunk_sequences)

        start = end - overlap

    return sequences


def compute_perplexity(loss):
    return torch.exp(loss)


def train(
    model,
    train_loader,
    val_loader,
    optimizer,
    criterion,
    scheduler,
    epochs=10,
    gradient_accumulation_steps=4,
):
    """
    Train the LongRoPE model.

    Args:
        model (nn.Module): The LongRoPE model to train.
        train_loader (DataLoader): DataLoader for training data.
        val_loader (DataLoader): DataLoader for validation data.
        optimizer (Optimizer): Optimizer for updating model parameters.
        criterion (nn.Module): Loss function.
        scheduler (LRScheduler): Learning rate scheduler.
        epochs (int): Number of training epochs.
        gradient_accumulation_steps (int): Number of steps to accumulate gradients.

    Returns:
        None
    """
    # Initialize the gradient scaler for mixed precision training
    scaler = GradScaler()

    # Variables for early stopping
    best_val_loss = float("inf")
    patience = 0
    max_patience = 3

    for epoch in range(epochs):
        model.train()
        total_loss = 0

        for i, (inputs, targets) in enumerate(train_loader):
            # Move data to the appropriate device (CPU or GPU)
            inputs, targets = (
                inputs.to(accelerator.device),
                targets.to(accelerator.device),
            )

            # Use mixed precision training
            with autocast():
                outputs = model(inputs)
                loss = criterion(outputs.permute(0, 2, 1), targets)
                # Normalize the loss to account for gradient accumulation
                loss = loss / gradient_accumulation_steps

            # Backpropagate and accumulate gradients
            scaler.scale(loss).backward()

            if (i + 1) % gradient_accumulation_steps == 0:
                # Update weights and reset gradients
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

            total_loss += loss.item()

        # Calculate average training loss and perplexity
        avg_train_loss = total_loss / len(train_loader)
        train_perplexity = compute_perplexity(avg_train_loss)

        # Validation step
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for inputs, targets in val_loader:
                inputs, targets = (
                    inputs.to(accelerator.device),
                    targets.to(accelerator.device),
                )
                outputs = model(inputs)
                loss = criterion(outputs.permute(0, 2, 1), targets)
                val_loss += loss.item()

        # Calculate average validation loss and perplexity
        avg_val_loss = val_loss / len(val_loader)
        val_perplexity = compute_perplexity(avg_val_loss)

        # Update learning rate
        scheduler.step()

        # Log metrics
        wandb.log(
            {
                "epoch": epoch,
                "train_loss": avg_train_loss,
                "train_perplexity": train_perplexity,
                "val_loss": avg_val_loss,
                "val_perplexity": val_perplexity,
                "learning_rate": scheduler.get_last_lr()[0],
            }
        )

        # Print epoch results
        print(
            f"Epoch {epoch+1}, Train Loss: {avg_train_loss:.4f}, Train Perplexity: {train_perplexity:.4f}, "
            f"Val Loss: {avg_val_loss:.4f}, Val Perplexity: {val_perplexity:.4f}"
        )

        # Save checkpoint
        accelerator.save_state(f"checkpoint_epoch_{epoch}.pt")

        # Early stopping
        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            patience = 0
            # Save best model
            accelerator.save_state("best_model.pt")
        else:
            patience += 1
            if patience >= max_patience:
                print("Early stopping triggered")
                break


# %%
def main():
    """Main function to setup and run training."""

    wandb.init(project="longrope", entity="your-entity-name")

    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    tokenizer.model_max_length = 2048000

    data = load_data("../data/raw/enwik8.gz")

    max_length = 65536
    overlap = 4096
    sequences = preprocess_data(data, tokenizer, max_length, overlap)

    targets = [seq[1:] + [tokenizer.eos_token_id] for seq in sequences]

    validate_targets(targets, tokenizer.vocab_size)

    dataset = CustomDataset(sequences, targets)
    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_dataset, val_dataset = torch.utils.data.random_split(
        dataset, [train_size, val_size]
    )

    train_loader = DataLoader(
        train_dataset, batch_size=8, shuffle=True, collate_fn=collate_fn
    )
    val_loader = DataLoader(val_dataset, batch_size=8, collate_fn=collate_fn)

    model = LongRoPEModel(
        d_model=4096,
        n_heads=32,
        num_layers=6,
        vocab_size=tokenizer.vocab_size,
        max_len=2048000,
    )

    optimizer = optim.AdamW(model.parameters(), lr=1e-4)
    criterion = nn.CrossEntropyLoss()
    scheduler = CosineAnnealingLR(optimizer, T_max=10)

    model, optimizer, train_loader, val_loader, scheduler = accelerator.prepare(
        model, optimizer, train_loader, val_loader, scheduler
    )

    extended_model = model.extend_context(
        data_path="../data/raw/enwik8.gz",
        target_length=2048000,
        max_sequence_length=65536,
        tokenizer=tokenizer,
        population_size=64,
        num_mutations=16,
        num_crossovers=16,
        max_iterations=10,
    )

    recovered_model = extended_model.recover_short_context(
        data_path="../data/raw/enwik8.gz",
        max_sequence_length=48192,
        tokenizer=tokenizer,
    )

    optimizer = optim.AdamW(recovered_model.parameters(), lr=1e-4)
    scheduler = CosineAnnealingLR(optimizer, T_max=10)

    train(recovered_model, train_loader, val_loader, optimizer, criterion, scheduler)

    wandb.finish()


if __name__ == "__main__":
    main()
