import os
import math
import numpy as np
import argparse
import torch
import trimesh
import open3d as o3d
from pytorch3d.loss import chamfer_distance
from random import randrange
from tqdm import tqdm
import sklearn.cluster
from torch.utils.data import DataLoader

import posa.vis_utils as vis_utils
import posa.general_utils as general_utils
import posa.data_utils as du
from posa.dataset import ProxDataset_txt, HUMANISE

from util.translate_obj_bbox import *

from contact_former.bridge_model import BridgeModel
from contact_former.contact_former import ContactFormer
from atiss.scripts.training_utils import load_config
from atiss.scene_synthesis.networks import build_network

"""
Running sample:
python test_contactformer.py ../data/proxd_valid/ --load_model ../training/contactformer/model_ckpt/best_model_recon_acc.pt --model_name contactformer --fix_ori --test_on_valid_set --output_dir ../test_output
python test_contactformer.py ../data/proxd_valid/ --load_model ../training/contactformer/model_ckpt/best_model_recon_acc.pt --model_name contactformer --fix_ori --single_seq_name MPH112_00151_01 --save_video --output_dir ../test_output
"""
def list_mean(list):
    acc = 0.
    for item in list:
        acc += item
    return acc / len(list)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("data_dir", type=str,
                        help="path to POSA_temp dataset dir")
    parser.add_argument("--load_model", type=str, default="training/contactformer/model_ckpt/best_model_recon_acc.pt",
                        help="checkpoint path to load")
    parser.add_argument("--posa_path", type=str, default="training/posa/model_ckpt/best_model_recon_acc.pt")
    parser.add_argument("--visualize", dest="visualize", action='store_const', const=True, default=False)
    parser.add_argument("--scene_dir", type=str, default="data/scenes",
                        help="the path to the scene mesh")
    parser.add_argument("--tpose_mesh_dir", type=str, default="data/mesh_ds",
                        help="the path to the tpose body mesh (primarily for loading faces)")
    parser.add_argument("--save_video", dest='save_video', action='store_const', const=True, default=False)
    parser.add_argument("--output_dir", type=str, default="../test_output",
                        help="the path to save test results")
    parser.add_argument("--cam_setting_path", type=str, default="posa/support_files/ScreenCamera_0.json",
                        help="the path to camera settings in open3d")
    parser.add_argument("--single_seq_name", type=str, default="BasementSittingBooth_00142_01")
    parser.add_argument("--test_on_train_set", dest='do_train', action='store_const', const=True, default=False)
    parser.add_argument("--test_on_valid_set", dest='do_valid', action='store_const', const=True, default=False)
    parser.add_argument("--model_name", type=str, default="default_model",
                        help="The name of the model we are testing. This is also the suffix for result text file name.")
    parser.add_argument("--fix_ori", dest='fix_ori', action='store_const', const=True, default=False)
    parser.add_argument("--encoder_mode", type=int, default=1,
                        help="Encoder mode (different number represents different versions of encoder)")
    parser.add_argument("--decoder_mode", type=int, default=1,
                        help="Decoder mode (different number represents different versions of decoder)")
    parser.add_argument("--n_layer", type=int, default=3)
    parser.add_argument("--n_head", type=int, default=4)
    parser.add_argument("--jump_step", type=int, default=8)
    parser.add_argument("--dim_ff", type=int, default=512)
    parser.add_argument("--f_vert", type=int, default=64)
    parser.add_argument("--max_frame", type=int, default=256)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--datatype", type=str, default="proxd")
    parser.add_argument("--cf_ckpt", type=str, default="training/contactformer/model_ckpt/best_model_recon_acc.pt", help="Path to pretrain ContactFormer")

    # Parse arguments and assign directories
    args = parser.parse_args()
    args_dict = vars(args)

    data_dir = args_dict['data_dir']
    scene_dir = args_dict['scene_dir']
    tpose_mesh_dir = args_dict['tpose_mesh_dir']
    ckpt_path = args_dict['load_model']
    save_video = args_dict['save_video']
    output_dir = args_dict['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    cam_path = args_dict['cam_setting_path']
    single_seq_name = args_dict['single_seq_name']
    model_name = args_dict['model_name']
    fix_ori = args_dict['fix_ori']
    encoder_mode = args_dict['encoder_mode']
    decoder_mode = args_dict['decoder_mode']
    do_train = args_dict['do_train']
    do_valid = args_dict['do_valid']
    visualize = args_dict['visualize']
    n_layer = args_dict['n_layer']
    n_head = args_dict['n_head']
    jump_step = args_dict['jump_step']
    max_frame = args_dict['max_frame']
    dim_ff = args_dict['dim_ff']
    f_vert = args_dict['f_vert']
    posa_path = args_dict['posa_path']
    seed = args_dict['seed']
    datatype = args_dict['datatype']
    cf_ckpt_path = args_dict['cf_ckpt']

    # Argument logic check
    if visualize and save_video:
        save_video = False
    if do_train or do_valid:
        save_video = False
        visualize = False

    device = torch.device("cuda")
    num_obj_classes = 8
    pnt_size = 1024
    # For fix_ori
    ds_weights = torch.tensor(np.load("posa/support_files/downsampled_weights.npy"))
    associated_joints = torch.argmax(ds_weights, dim=1)
    torch.manual_seed(seed)

    if datatype == 'proxd':
        valid_dataset = ProxDataset_txt(data_dir, max_frame=max_frame, fix_orientation=fix_ori,
                                    step_multiplier=1, jump_step=jump_step)
        valid_data_loader = DataLoader(valid_dataset, batch_size=1, shuffle=False, num_workers=0)
    else:
        valid_dataset = HUMANISE(data_dir, max_frame=max_frame, fix_orientation=fix_ori,
                                    step_multiplier=1, jump_step=jump_step)
        valid_data_loader = DataLoader(valid_dataset, batch_size=1, shuffle=False, num_workers=0)
    
    # Create ContactFormer and ATISS model
    cf_model = ContactFormer(seg_len=max_frame, encoder_mode=encoder_mode, decoder_mode=decoder_mode,
                          n_layer=n_layer, n_head=n_head, f_vert=f_vert, dim_ff=dim_ff, d_hid=512,
                          posa_path=posa_path).to(device)
    cf_model.eval()
    cf_checkpoint = torch.load(cf_ckpt_path)
    cf_model.load_state_dict(cf_checkpoint['model_state_dict'])

    # Setup for ATISS model
    config_path = os.path.join("atiss", "config", "bedrooms_eval_config.yaml")
    config = load_config(config_path)
    num_classes = valid_dataset.max_cats
    atiss_model, _, _ = build_network(
        num_classes + 7, num_classes,
        config, None, device=device
    )
    atiss_model.eval()
    checkpoint = torch.load(ckpt_path)
    atiss_model.load_state_dict(checkpoint['model_state_dict'])
    model = BridgeModel(atiss_model, cf_model, datatype, num_classes, device=device)

    # Setup names for output files
    context_dir = os.path.join(data_dir, 'context')
    files = os.listdir(context_dir)
    lookup_tab = dict()
    for file in files:
        reduced_file = file.split('.')[0]

        with open(os.path.join(context_dir, file), 'r') as f:
            prompt = f.readlines()[0].strip()
            lookup_tab[prompt] = reduced_file
    
    seq_name_list = []
    chamfer_list = []
    total_acc = []
    seq_class_acc = [[] for _ in range(num_obj_classes)]
    
    f = open(os.path.join(output_dir, "results.txt"), "w+")
    for mask, given_objs, given_cats, target_obj, target_cat, y in tqdm(valid_data_loader):
        # Loop over video frames to get predictions
        # Metrics for semantic labels
        # Switch to cuda
        mask = mask.to(device)
        given_objs = given_objs.to(device)
        given_cats = given_cats.to(device)
        target_obj = target_obj.to(device)
        target_cat = target_cat.to(device)

        chamfer_s = 0
        class_acc_list = [[] for _ in range(num_obj_classes)]
        class_acc = dict()

        # Compute output for joint ATISS & ContactFormer model
        output_obj = model(given_objs, given_cats, mask)

        # Get the output boxes
        sizes_x, sizes_y, sizes_z, translations_x, translations_y, translations_z, angles, class_labels = output_obj.members
        pred_sizes = torch.cat([sizes_x, sizes_y, sizes_z], dim=-1)
        pred_translation = torch.cat([translations_x, translations_y, translations_z], dim=-1)

        # Get sampling points from bounding box
        pred = translate_bbox_obj(pred_translation, pred_sizes)
        pr_pnts = pred
        gt_pnts = target_obj

        loss, loss_normals = chamfer_distance(pr_pnts, gt_pnts.float().to(device))
        chamfer_s += loss
        chamfer_list.append(chamfer_s)

        # Calculate for categorical
        class_labels = class_labels.squeeze(1).argmax(dim=-1)
        target_cat = torch.argmax(target_cat, dim=1)
        acc = (class_labels==target_cat).sum().item()
        total_acc.append(acc)
        # visualizer.destroy_window()
        out_file = lookup_tab[y[0]]
        f.write("Chamfer distance for seq {}: {:.4f}".format(out_file, chamfer_s) + '\n')

        # Write predicted points to files
        if not os.path.exists(os.path.join(output_dir, 'predictions')):
            os.makedirs(os.path.join(output_dir, 'predictions'))
        with open(os.path.join(output_dir, 'predictions', out_file + '.npy'), 'wb') as fp:
            pred = pred[0].detach().cpu().numpy()
            np.save(fp, pred)

    f.write("Final Chamfer distance: {:.4f}".format(list_mean(chamfer_list)) + '\n')
    f.write("Category accuracy: {:.4f}".format(list_mean(total_acc)) + '\n')
    f.close()