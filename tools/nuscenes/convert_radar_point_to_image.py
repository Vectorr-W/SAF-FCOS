# nuScenes dev-kit.
# Code written by Sergi Adipraja Widjaja, 2019.
# Licensed under the Creative Commons [see license.txt]

"""
Export 2D annotations (xmin, ymin, xmax, ymax) from re-projections of our annotated 3D bounding boxes to a .json file.

Note: Projecting tight 3d boxes to 2d generally leads to non-tight boxes.
      Furthermore it is non-trivial to determine whether a box falls into the image, rather than behind or around it.
      Finally some of the objects may be occluded by other objects, in particular when the lidar can see them, but the
      cameras cannot.
"""

import argparse
import json
import os
import sys
from concurrent import futures
from typing import List, Tuple, Union

import cv2
import numpy as np
from PIL import Image
from nuscenes.nuscenes import NuScenes
from nuscenes.utils.data_classes import LidarPointCloud, RadarPointCloud
from nuscenes.utils.geometry_utils import view_points
from pyquaternion.quaternion import Quaternion
from shapely.geometry import MultiPoint, box

_DISTANCE_RANGE = [0, 250]
_SPEED_RANGE = [-33, 33]
_RCS_RANGE = [0, 100]
_UNKNOWN_AREA_VALUE = 0
_UNKNOWN_AREA_RANGE = [0, 126]
_RADIUS = 7


# Print iterations progress (thanks StackOverflow)
def print_progress(iteration, total, prefix='', suffix='', decimals=1, bar_length=100):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        bar_length   - Optional  : character length of bar (Int)
    """
    formatStr = "{0:." + str(decimals) + "f}"
    percents = formatStr.format(100 * (iteration / float(total)))
    filledLength = int(round(bar_length * iteration / float(total)))
    bar = '' * filledLength + '-' * (bar_length - filledLength)
    sys.stdout.write('\r%s |%s| %s%s %s' % (prefix, bar, percents, '%', suffix)),
    if iteration == total:
        sys.stdout.write('\x1b[2K\r')
    sys.stdout.flush()


def post_process_coords(corner_coords: List,
                        im_size: Tuple[int, int] = (1600, 900)) -> Union[Tuple[float, float, float, float], None]:
    """
    Get the intersection of the convex hull of the reprojected bbox corners and the image canvas, return None if no
    intersection.
    :param corner_coords: Corner coordinates of reprojected bounding box.
    :param im_size: Size of the image canvas.
    :return: Intersection of the convex hull of the 2D box corners and the image canvas.
    """
    polygon_from_2d_box = MultiPoint(corner_coords).convex_hull
    img_canvas = box(0, 0, im_size[0], im_size[1])

    if polygon_from_2d_box.intersects(img_canvas):
        img_intersection = polygon_from_2d_box.intersection(img_canvas)
        intersection_coords = np.array([coord for coord in img_intersection.exterior.coords])

        min_x = min(intersection_coords[:, 0])
        min_y = min(intersection_coords[:, 1])
        max_x = max(intersection_coords[:, 0])
        max_y = max(intersection_coords[:, 1])

        return min_x, min_y, max_x, max_y
    else:
        return None


def map_point_cloud_to_image(point_sensor_token, camera_token, min_dist=1.0):
    """
    Given a point sensor (lidar/radar) token and camera sample_data token, load point-cloud and map it to the image
    plane.
    :param point_sensor_token: Lidar/radar sample_data token.
    :param camera_token: Camera sample_data token.
    :param min_dist: Distance from the camera below which points are discarded.
    :return (pointcloud <np.float: 2, n)>.
    """
    cam = nusc.get('sample_data', camera_token)
    point_sensor = nusc.get('sample_data', point_sensor_token)
    pcl_path = os.path.join(nusc.dataroot, point_sensor['filename'])
    if point_sensor['sensor_modality'] == 'lidar':
        pc = LidarPointCloud.from_file(pcl_path)
    else:
        # For the radar data format,
        # please refer to https://github.com/ApolloAuto/apollo/blob/master/modules/drivers/proto/conti_radar.proto
        pc = RadarPointCloud.from_file(pcl_path)

    # Points live in the point sensor frame. So they need to be transformed via global to the image plane.
    # First step: transform the point-cloud to the ego vehicle frame for the timestamp of the sweep.
    cs_record = nusc.get('calibrated_sensor', point_sensor['calibrated_sensor_token'])
    pc.rotate(Quaternion(cs_record['rotation']).rotation_matrix)
    pc.translate(np.array(cs_record['translation']))

    # Second step: transform to the global frame.
    pose_record = nusc.get('ego_pose', point_sensor['ego_pose_token'])
    pc.rotate(Quaternion(pose_record['rotation']).rotation_matrix)
    pc.translate(np.array(pose_record['translation']))

    # Third step: transform into the ego vehicle frame for the timestamp of the image.
    pose_record = nusc.get('ego_pose', cam['ego_pose_token'])
    pc.translate(-np.array(pose_record['translation']))
    pc.rotate(Quaternion(pose_record['rotation']).rotation_matrix.T)

    # Fourth step: transform into the camera.
    cs_record = nusc.get('calibrated_sensor', cam['calibrated_sensor_token'])
    pc.translate(-np.array(cs_record['translation']))
    pc.rotate(Quaternion(cs_record['rotation']).rotation_matrix.T)

    # Fifth step: actually take a "picture" of the point cloud.
    # Grab the depths (camera frame z axis points away from the camera).
    depths = pc.points[2, :]

    # Take the actual picture (matrix multiplication with camera-matrix + renormalization).
    points = view_points(pc.points[:3, :], np.array(cs_record['camera_intrinsic']), normalize=True)
    pc = np.copy(pc.points)

    # Remove points that are either outside or behind the camera. Leave a margin of 1 pixel for aesthetic reasons.
    # Also make sure points are at least 1m in front of the camera to avoid seeing the lidar points on the camera
    # casing for non-keyframes which are slightly out of sync.
    mask = np.ones(depths.shape[0], dtype=bool)
    mask = np.logical_and(mask, depths > min_dist)
    mask = np.logical_and(mask, points[0, :] > 1)
    mask = np.logical_and(mask, points[0, :] < cam['width'] - 1)
    mask = np.logical_and(mask, points[1, :] > 1)
    mask = np.logical_and(mask, points[1, :] < cam['height'] - 1)
    pc[:2, :] = points[:2, :]
    pc = pc[:, mask]

    return pc


def draw_pc_image(pc, save_path, radius, im_height=900, im_width=1600):
    img_b = np.zeros((im_height, im_width), np.uint8)
    img_g = np.zeros((im_height, im_width), np.uint8)
    img_r = np.zeros((im_height, im_width), np.uint8)
    his_mask = np.zeros((im_height, im_width), np.uint8)
    pc = pc[:, pc[2, :].argsort()]
    num_points = pc.shape[1]
    # Line thickness of 2 px
    thickness = -1

    point_id = 0
    for i in range(num_points):
        center_coordinates = (int(pc[0, i]), int(pc[1, i]))
        depth = pc[2, i]
        vx = pc[6, i]
        vy = pc[7, i]
        if (depth > _DISTANCE_RANGE[0]) and (depth < _DISTANCE_RANGE[1]):
            v = np.sqrt(vx ** 2 + vy ** 2)
            if (v > _SPEED_RANGE[0]) and (v < _SPEED_RANGE[1]):
                point_id += 1
                red = int(depth / 250 * 128 + 127)
                green = int((vx + 20) / 40 * 128 + 127)
                blue = int((vy + 20) / 40 * 128 + 127)
                color = (blue, green, red)
                color = np.asarray(color).astype(np.uint8)
                cur_mask = np.zeros((900, 1600), np.uint8)
                cur_mask = cv2.circle(cur_mask, center_coordinates, radius, 1, thickness)
                save_cur_mask = cur_mask - cur_mask * his_mask
                img_b = img_b + save_cur_mask * color[2]
                img_g = img_g + save_cur_mask * color[1]
                img_r = img_r + save_cur_mask * color[0]
                his_mask = his_mask + save_cur_mask

    im = np.stack([img_r, img_g, img_b], axis=2)
    image = Image.fromarray(im, 'RGB')
    image.save(save_path)
    norm_info = {}

    norm_save_path = save_path.replace('.png', '.json')
    # convert from integers to floats
    im = im.astype('float64')
    means = im.mean(axis=(0, 1), dtype='float64')
    stds = im.std(axis=(0, 1), dtype='float64')
    mean = np.reshape(means, [3, 1])
    std = np.reshape(stds, [3, 1])
    norm_info['mean'] = (mean[0, 0], mean[1, 0], mean[2, 0])
    norm_info['std'] = (std[0, 0], std[1, 0], std[2, 0])
    with open(norm_save_path, 'w') as f:
        json.dump(norm_info, f, sort_keys=True, indent=4)


def get_pc_info(sample_data_token):
    """
    Get the 2D annotation records for a given `sample_data_token`.
    :param sample_data_token: Sample data token belonging to a keyframe.
    """

    # Get the sample data and the sample corresponding to that sample data.
    sd_rec = nusc.get('sample_data', sample_data_token)

    if not sd_rec['is_key_frame']:
        raise ValueError('The 2D re-projections are available only for keyframes.')

    s_rec = nusc.get('sample', sd_rec['sample_token'])

    point_sensor_token = s_rec['data']['RADAR_FRONT']
    pc_rec = nusc.get('sample_data', point_sensor_token)
    pcd_path = os.path.join(nusc.dataroot, pc_rec['filename'].replace('samples', 'pc'))
    if os.path.isfile(pcd_path):
        return

    # Get sparse point cloud image and save it
    pc = map_point_cloud_to_image(point_sensor_token, sample_data_token)
    with open(pcd_path, 'w') as f:
        for i in range(pc.shape[0]):
            line_info = pc[i, :]
            line_info = list(line_info)
            line_info = ['%.3f' % item for item in line_info]
            line_info = ' '.join(line_info)
            f.write(line_info + '\n')


def convert_pcd_file(sample_data_token, radius):
    # Get the sample data and the sample corresponding to that sample data.
    sd_rec = nusc.get('sample_data', sample_data_token)

    if not sd_rec['is_key_frame']:
        raise ValueError('The 2D re-projections are available only for keyframes.')

    s_rec = nusc.get('sample', sd_rec['sample_token'])

    point_sensor_token = s_rec['data']['RADAR_FRONT']
    pc_rec = nusc.get('sample_data', point_sensor_token)
    pcd_path = os.path.join(nusc.dataroot, pc_rec['filename'].replace('samples', 'pc'))
    save_path = os.path.join(nusc.dataroot,
                             pc_rec['filename'].replace('samples', 'imagepc_%02d' % radius).replace('pcd', 'png'))
    save_folder = os.path.dirname(save_path)
    if os.path.isfile(save_path):
        return

    if not os.path.isdir(save_folder):
        os.makedirs(save_folder)
    pc = np.loadtxt(pcd_path)
    if len(pc.shape) == 1:
        if pc.shape[0] > 0:
            pc = np.expand_dims(pc, axis=1)
            draw_pc_image(pc, save_path, radius)
    else:
        draw_pc_image(pc, save_path, radius)


def run():
    # Get tokens for all camera images.
    sample_data_camera_tokens = [s['token'] for s in nusc.sample_data if (s['channel'] == 'CAM_FRONT') and
                                 s['is_key_frame']]

    # Loop through the records and get front radar point pc info by Multi Thread.
    print("Generating 2D re-projections of the nuScenes dataset")
    num_threads = 40
    num_tokens = len(sample_data_camera_tokens)
    with futures.ProcessPoolExecutor(max_workers=num_threads) as executor:
        fs = [executor.submit(get_pc_info, token) for token in
              sample_data_camera_tokens]
        for i, f in enumerate(futures.as_completed(fs)):
            # Write progress to error so that it can be seen
            print_progress(i, num_tokens, prefix=nuScenes_version, suffix='Done ', bar_length=40)

    print("Generating 2D radar image by depth vx vy")
    num_threads = 40
    num_tokens = len(sample_data_camera_tokens)
    for radius in radius_list:
        with futures.ProcessPoolExecutor(max_workers=num_threads) as executor:
            fs = [executor.submit(convert_pcd_file, token, radius) for token in
                  sample_data_camera_tokens]
            for i, f in enumerate(futures.as_completed(fs)):
                # Write progress to error so that it can be seen
                print_progress(i, num_tokens, prefix=nuScenes_version, suffix='Done ', bar_length=40)

    if not os.path.isfile(os.path.join(args.dataroot, args.version, args.filename)):
        # Save to a .json file.
        print("Combine the individual json files")
        dest_path = os.path.join(args.dataroot, args.version)
        if not os.path.exists(dest_path):
            os.makedirs(dest_path)
        with open(os.path.join(args.dataroot, args.version, args.filename), 'w') as fh:
            reprojections = []
            for token in sample_data_camera_tokens:
                # Get the sample data and the sample corresponding to that sample data.
                sd_rec = nusc.get('sample_data', token)
                json_path = os.path.join(nusc.dataroot,
                                         sd_rec['filename'].replace('samples', 'json').replace('jpg', 'json'))
                with open(json_path, 'r') as f:
                    reprojection_records = json.load(f)
                reprojections.append(reprojection_records)
            json.dump(reprojections, fh, sort_keys=True, indent=4)

        print("Saved the 2D re-projections under {}".format(os.path.join(args.dataroot, args.version, args.filename)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Export 2D annotations from reprojections to a .json file.',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--dataroot', type=str, default='/home/citybuster/Data/nuScenes/',
                        help="Path where nuScenes is saved.")
    parser.add_argument('--version', type=str, default='v1.0-trainval', help='Dataset version.')
    parser.add_argument('--filename', type=str, default='image_pc_annotations.json', help='Output filename.')
    parser.add_argument('--visibilities', type=str, default=['1', '2', '3', '4'],
                        help='Visibility bins, the higher the number the higher the visibility.', nargs='+')
    args = parser.parse_args()

    # Make dirs to save pc info, which is extracted from pcd file of front radar
    if not os.path.exists(os.path.join(args.dataroot, 'pc')):
        os.makedirs(os.path.join(args.dataroot, 'pc', 'RADAR_FRONT'))

    nuScenes_sets = ['v1.0-test', 'v1.0-trainval']
    radius_list = [1, 3, 5, 7, 9, 11]
    for index, nuScenes_version in enumerate(nuScenes_sets):
        nusc = NuScenes(dataroot=args.dataroot, version=nuScenes_version)
        run()

