import numpy as np
import cv2

import torch
import torch.nn.functional as F

from tapnet import tapir_model

if torch.cuda.is_available():
    device = torch.device('cuda')
else:
    device = torch.device('cpu')

def draw_points(frame, points, visible, colors):
    for i in range(points.shape[0]):
        if not visible[i]:
            continue

        point = points[i,:]
        color = colors[i,:]
        cv2.circle(frame,
                   (int(point[0]), int(point[1])),
                   5,
                   (int(color[0]), int(color[1]), int(color[2])),
                   -1)
    return frame

def preprocess_frame(frame, resize=(256, 256)):

    input = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    input = cv2.resize(input, resize)
    input = input[np.newaxis, :, :, :].astype(np.float32)

    input = torch.tensor(input).to(device)
    input = input.float()
    input = input / 255 * 2 - 1
    input = input.permute(0, 3, 1, 2)

    return input

def sample_random_points(frame_max_idx, height, width, num_points):
    """Sample random points with (time, height, width) order."""
    x = np.linspace(0, width - 1, int(np.sqrt(num_points))).astype(np.int32)
    y = np.linspace(0, height - 1, int(np.sqrt(num_points))).astype(np.int32)
    x, y = np.meshgrid(x, y)
    x = np.expand_dims(x.flatten(), -1)
    y = np.expand_dims(y.flatten(), -1)
    t = np.random.randint(0, frame_max_idx + 1, (num_points, 1))
    points = np.concatenate((t, y, x), axis=-1).astype(np.int32)  # [num_points, 3]
    return points


def postprocess_occlusions(occlusions, expected_dist):
    visibles = (1 - F.sigmoid(occlusions)) * (1 - F.sigmoid(expected_dist)) > 0.5
    return visibles


def online_model_init(frame, query_points):
    """Initialize query features for the query points."""
    frame = preprocess_frame(frame, resize=(resize_height, resize_width))
    feature_grid, hires_feats = model.get_feature_grids(frame)
    query_feats, hires_query_feats = model.get_query_features(
        query_points=query_points,
        feature_grid=feature_grid,
        hires_feats=hires_feats,
    )
    return query_feats, hires_query_feats


@torch.inference_mode()
def online_model_predict(frame, query_feats, hires_query_feats, causal_context):
    """Compute point tracks and occlusions given frame and query points."""
    frame = preprocess_frame(frame, resize=(resize_height, resize_width))
    feature_grid, hires_feats = model.get_feature_grids(frame)
    trajectories = model.estimate_trajectories(
        (resize_height, resize_width),
        feature_grid=feature_grid,
        hires_feats=hires_feats,
        query_feats=query_feats,
        hires_query_feats=hires_query_feats,
        query_chunk_size=64,
        causal_context=causal_context,
        get_causal_context=True,
    )
    causal_context = trajectories['causal_context']

    # Take only the predictions for the final resolution.
    # For running on higher resolution, it's typically better to average across
    # resolutions.
    tracks = trajectories['tracks'][-1]
    occlusions = trajectories['occlusion'][-1]
    uncertainty = trajectories['expected_dist'][-1]
    visibles = postprocess_occlusions(occlusions, uncertainty)
    return tracks, visibles, causal_context

if __name__ == '__main__':
    resize_height = 256
    resize_width = 256
    num_points = 256

    model = tapir_model.TAPIR(pyramid_level=1, use_casual_conv=True, initial_resolution=(resize_height, resize_width))
    model.load_state_dict(torch.load('causal_bootstapir_checkpoint.pt'))
    model = model.to(device)
    model = model.eval()

    cap = cv2.VideoCapture('horsejump-high.mp4')
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    query_points = sample_random_points(0, resize_height, resize_width, num_points)
    query_points = torch.tensor(query_points).to(device)
    point_colors = np.random.randint(0, 255, (num_points, 3))

    # Initialize query features
    ret, frame = cap.read()
    query_feats, hires_query_feats = online_model_init(frame, query_points[None])

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    causal_state = model.construct_initial_causal_state(query_points.shape[0], len(query_feats) - 1)
    for i in range(len(causal_state)):
        for k, v in causal_state[i].items():
            causal_state[i][k] = v.to(device)

    predictions = []
    frames = []
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        # Note: we add a batch dimension.
        tracks, visibles, causal_state = online_model_predict(frame=frame, query_feats=query_feats, hires_query_feats=hires_query_feats, causal_context=causal_state)

        frames.append(frame)
        predictions.append({'tracks': tracks, 'visibles': visibles})

        visibles = visibles.cpu().numpy().squeeze()
        tracks = tracks.cpu().numpy().squeeze()
        tracks[:, 0] = tracks[:, 0] * width / resize_width
        tracks[:, 1] = tracks[:, 1] * height / resize_height

        frame = draw_points(frame, tracks, visibles, point_colors)

        cv2.imshow('frame', frame)
        if cv2.waitKey(30) & 0xFF == ord('q'):
            break

