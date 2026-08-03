"""Pure-Python reference for the deterministic MLP-DP mathematical contract."""
from __future__ import annotations

from math import exp

RANKS = 8
GPUS_PER_RANK = 2
ITERATIONS = 100
SAMPLES = 128
FEATURES = 4
HIDDEN = 6
CLASSES = 3
SAMPLES_PER_GPU = SAMPLES // (RANKS * GPUS_PER_RANK)
W1 = FEATURES * HIDDEN
B1 = HIDDEN
W2 = HIDDEN * CLASSES
B2 = CLASSES
PARAMETERS = W1 + B1 + W2 + B2
LEARNING_RATE = 0.05


def w1(feature: int, hidden: int) -> int:
    return feature * HIDDEN + hidden


def b1(hidden: int) -> int:
    return W1 + hidden


def w2(hidden: int, output: int) -> int:
    return W1 + B1 + hidden * CLASSES + output


def b2(output: int) -> int:
    return W1 + B1 + W2 + output


def shard(rank: int, gpu: int) -> list[tuple[list[float], int]]:
    """Return the exact, rank-stable input samples assigned to one GPU worker."""
    result: list[tuple[list[float], int]] = []
    for local in range(SAMPLES_PER_GPU):
        sample = rank + RANKS * (gpu + GPUS_PER_RANK * local)
        inputs = [((sample + 3) * (feature + 5) % 17 - 8) / 8.0 for feature in range(FEATURES)]
        result.append((inputs, sample % CLASSES))
    return result


def local_gradient(model: list[float], samples: list[tuple[list[float], int]]) -> list[float]:
    gradient = [0.0] * PARAMETERS
    for inputs, label in samples:
        hidden_pre = [model[b1(h)] + sum(inputs[f] * model[w1(f, h)] for f in range(FEATURES))
                      for h in range(HIDDEN)]
        hidden = [max(value, 0.0) for value in hidden_pre]
        logits = [model[b2(output)] + sum(hidden[h] * model[w2(h, output)] for h in range(HIDDEN))
                  for output in range(CLASSES)]
        normalizer = sum(exp(value) for value in logits)
        probabilities = [exp(value) / normalizer for value in logits]
        errors = [probabilities[output] - (1.0 if output == label else 0.0)
                  for output in range(CLASSES)]
        for output, error in enumerate(errors):
            gradient[b2(output)] += error
            for h in range(HIDDEN):
                gradient[w2(h, output)] += hidden[h] * error
        for h in range(HIDDEN):
            backprop = sum(errors[output] * model[w2(h, output)] for output in range(CLASSES))
            if hidden[h] == 0.0:
                backprop = 0.0
            gradient[b1(h)] += backprop
            for feature in range(FEATURES):
                gradient[w1(feature, h)] += inputs[feature] * backprop
    return gradient


def rank_ordered_reduce(rank_gradients: list[list[float]]) -> list[float]:
    """Reduce rank gradients in the former serial rank-0 order."""
    total = list(rank_gradients[0])
    for rank in range(1, RANKS):
        total = [total[index] + rank_gradients[rank][index] for index in range(PARAMETERS)]
    return total


def binary_tree_reduce(rank_gradients: list[list[float]]) -> list[float]:
    """Match the deterministic contiguous-subtree reduction in mlp_dp_cpu.cpp."""
    partial = [list(gradient) for gradient in rank_gradients]
    stride = 1
    while stride < RANKS:
        for parent in range(0, RANKS, 2 * stride):
            child = parent + stride
            partial[parent] = [
                partial[parent][index] + partial[child][index]
                for index in range(PARAMETERS)
            ]
        stride *= 2
    return partial[0]


def reference_model(reduction: str = "rank_ordered") -> list[float]:
    """Run synchronous SGD with either serial or tree gradient reduction."""
    reducers = {
        "rank_ordered": rank_ordered_reduce,
        "binary_tree": binary_tree_reduce,
    }
    try:
        reduce_gradients = reducers[reduction]
    except KeyError as error:
        raise ValueError(f"unsupported reduction: {reduction}") from error

    model = [0.01 * (index % 7 - 3) for index in range(PARAMETERS)]
    for _ in range(ITERATIONS):
        rank_gradients: list[list[float]] = []
        for rank in range(RANKS):
            left = local_gradient(model, shard(rank, 0))
            right = local_gradient(model, shard(rank, 1))
            rank_gradients.append([left[index] + right[index] for index in range(PARAMETERS)])
        total = reduce_gradients(rank_gradients)
        model = [model[index] - LEARNING_RATE * total[index] / SAMPLES
                 for index in range(PARAMETERS)]
    return model


if __name__ == "__main__":
    serial_model = reference_model("rank_ordered")
    tree_model = reference_model("binary_tree")
    maximum_error = max(abs(left - right) for left, right in zip(serial_model, tree_model))
    print(f"maximum_absolute_difference={maximum_error:.17g}")
