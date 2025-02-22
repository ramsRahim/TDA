import random
import argparse
from tqdm import tqdm
from datetime import datetime

import torch
import torch.nn.functional as F
import operator

import clip
from utils import *
import time

def get_arguments():
    """Get arguments of the test-time adaptation."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', dest='config', required=True, help='settings of TDA on specific dataset in yaml format.')
    parser.add_argument('--datasets', dest='datasets', type=str, required=True, help="Datasets to process, separated by a slash (/). Example: I/A/V/R/S")
    parser.add_argument('--data-root', dest='data_root', type=str, default='./dataset/', help='Path to the datasets directory. Default is ./dataset/')
    parser.add_argument('--backbone', dest='backbone', type=str, choices=['RN50', 'ViT-B/16'], required=True, help='CLIP model backbone to use: RN50 or ViT-B/16.')

    args = parser.parse_args()

    return args

def update_cache(cache, pred, features_loss, shot_capacity, include_prob_map=False):
    """Update cache with new features and loss, maintaining the maximum shot capacity."""
    with torch.no_grad():
        item = features_loss if not include_prob_map else features_loss[:2] + [features_loss[2]]
        if pred in cache:
            if len(cache[pred]) < shot_capacity:
                cache[pred].append(item)
            elif features_loss[1] < cache[pred][-1][1]:
                cache[pred][-1] = item
            cache[pred] = sorted(cache[pred], key=operator.itemgetter(1))
        else:
            cache[pred] = [item]

def compute_cache_logits(image_features, cache, alpha, beta, clip_weights, neg_mask_thresholds=None):
    """Compute logits using positive/negative cache."""
    with torch.no_grad():
        cache_keys = []
        cache_values = []
        for class_index in sorted(cache.keys()):
            for item in cache[class_index]:
                cache_keys.append(item[0])
                if neg_mask_thresholds:
                    cache_values.append(item[2])
                else:
                    cache_values.append(class_index)

        cache_keys = torch.cat(cache_keys, dim=0).permute(1, 0)
        if neg_mask_thresholds:
            cache_values = torch.cat(cache_values, dim=0)
            cache_values = (((cache_values > neg_mask_thresholds[0]) & (cache_values < neg_mask_thresholds[1])).type(torch.int8)).cuda().half()
        else:
            cache_values = (F.one_hot(torch.Tensor(cache_values).to(torch.int64), num_classes=clip_weights.size(1))).cuda().half()

        affinity = image_features @ cache_keys
        cache_logits = ((-1) * (beta - beta * affinity)).exp() @ cache_values
        return alpha * cache_logits

def get_tensor_size(tensor):
    """
    Calculate the size of a PyTorch tensor in bytes.

    Args:
        tensor (torch.Tensor): The input tensor.

    Returns:
        int: The size of the tensor in bytes.
    """
    if not isinstance(tensor, torch.Tensor):
        raise ValueError("Input must be a PyTorch tensor.")
    
    # print(tensor.element_size())
    return tensor.element_size() * tensor.numel()


def compute_real_cache_size(cache):
    """
    Compute the real memory size of a cache (positive or negative) in bytes.
    """
    total_size = 0
    # print(cache.keys())
    for class_entries in cache.values():
        for item in class_entries:
            feature_size = get_tensor_size(item[0])  # Feature embedding
            metadata_size = sum(get_tensor_size(x) for x in item[1:])  # Loss, scale, zero point, etc.
            total_size += feature_size + metadata_size
    return total_size


def run_test_tda(pos_cfg, neg_cfg, loader, clip_model, clip_weights, log_file):
    with open(log_file, 'w') as log:
        with torch.no_grad():
            pos_cache, neg_cache, accuracies = {}, {}, []
            total_lookup_time = 0.0
            total_inference_time = 0.0
            num_lookups = 0

            # Unpack all hyperparameters
            pos_enabled, neg_enabled = pos_cfg['enabled'], neg_cfg['enabled']
            if pos_enabled:
                pos_params = {k: pos_cfg[k] for k in ['shot_capacity', 'alpha', 'beta']}
            if neg_enabled:
                neg_params = {k: neg_cfg[k] for k in ['shot_capacity', 'alpha', 'beta', 'entropy_threshold', 'mask_threshold']}

            # Test-time adaptation
            for i, (images, target) in enumerate(tqdm(loader, desc='Processed test images: ')):
                start_time = time.time()  # Start inference time measurement

                image_features, clip_logits, loss, prob_map, pred = get_clip_logits(images, clip_model, clip_weights)
                target, prop_entropy = target.cuda(), get_entropy(loss, clip_weights)

                if pos_enabled:
                    update_cache(pos_cache, pred, [image_features, loss], pos_params['shot_capacity'])

                if neg_enabled and neg_params['entropy_threshold']['lower'] < prop_entropy < neg_params['entropy_threshold']['upper']:
                    update_cache(neg_cache, pred, [image_features, loss, prob_map], neg_params['shot_capacity'], True)

                final_logits = clip_logits.clone()

                # Measure lookup time for cache
                lookup_start = time.time()
                if pos_enabled and pos_cache:
                    final_logits += compute_cache_logits(image_features, pos_cache, pos_params['alpha'], pos_params['beta'], clip_weights)
                if neg_enabled and neg_cache:
                    final_logits -= compute_cache_logits(image_features, neg_cache, neg_params['alpha'], neg_params['beta'], clip_weights, 
                                                         (neg_params['mask_threshold']['lower'], neg_params['mask_threshold']['upper']))
                lookup_end = time.time()

                lookup_time = lookup_end - lookup_start
                total_lookup_time += lookup_time
                num_lookups += 1

                acc = cls_acc(final_logits, target)
                accuracies.append(acc)

                inference_time = time.time() - start_time  # Measure total inference time
                total_inference_time += inference_time

                # Monitor the KV table size
                if i % 1000 == 0:
                    pos_cache_size = compute_real_cache_size(pos_cache)
                    neg_cache_size = compute_real_cache_size(neg_cache)
                    avg_lookup_time = total_lookup_time / num_lookups if num_lookups > 0 else 0
                    avg_inference_time = total_inference_time / (i + 1)

                    log.write(f"---- Iteration {i} ----\n")
                    log.write(f"Positive Cache Size: {pos_cache_size} bytes\n")
                    log.write(f"Negative Cache Size: {neg_cache_size} bytes\n")
                    log.write(f"Average Cache Lookup Time: {avg_lookup_time:.6f} seconds\n")
                    log.write(f"Average Inference Time: {avg_inference_time:.6f} seconds\n")
                    log.write(f"---- TDA's test accuracy: {sum(accuracies)/len(accuracies):.2f}. ----\n\n")

            # Final logging
            avg_lookup_time = total_lookup_time / num_lookups if num_lookups > 0 else 0
            avg_inference_time = total_inference_time / len(loader)

            log.write("---- Final Results ----\n")
            log.write(f"Positive Cache Size: {compute_real_cache_size(pos_cache)} bytes\n")
            log.write(f"Negative Cache Size: {compute_real_cache_size(neg_cache)} bytes\n")
            log.write(f"Average Cache Lookup Time: {avg_lookup_time:.6f} seconds\n")
            log.write(f"Average Inference Time: {avg_inference_time:.6f} seconds\n")
            log.write(f"TDA's test accuracy: {sum(accuracies) / len(accuracies):.2f}.\n")

    return sum(accuracies) / len(accuracies)


def main():
    args = get_arguments()
    config_path = args.config

    # Initialize CLIP model
    clip_model, preprocess = clip.load(args.backbone)
    clip_model.eval()

    # Set random seed
    random.seed(1)
    torch.manual_seed(1)


    # Run TDA on each dataset
    datasets = args.datasets.split('/')
    for dataset_name in datasets:
        print(f"Processing {dataset_name} dataset.")
        
        cfg = get_config_file(config_path, dataset_name)
        print("\nRunning dataset configurations:")
        print(cfg, "\n")
        
        test_loader, classnames, template = build_test_data_loader(dataset_name, args.data_root, preprocess)
        clip_weights = clip_classifier(classnames, template, clip_model)
        log_file = f"logs_{dataset_name}_tda_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        acc = run_test_tda(cfg['positive'], cfg['negative'], test_loader, clip_model, clip_weights, log_file)

if __name__ == "__main__":
    main()
