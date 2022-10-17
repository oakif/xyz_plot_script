from collections import namedtuple
from copy import copy
from itertools import permutations, chain
import json
import os
import random
import csv
from io import StringIO
import shutil
import numpy as np

import modules.scripts as scripts
import gradio as gr

from modules import images, hypernetwork
from modules.processing import process_images, get_correct_sampler
from modules.shared import opts, state
import modules.shared as shared
import modules.sd_samplers
import modules.sd_models
import re

def apply_field(field):
    def fun(p, x, xs):
        setattr(p, field, x)

    return fun

def writefile(dir, fn, contents, append=False, encoding='utf8'):
    with open(os.path.join(dir, fn), 'w' if not append else 'a', encoding=encoding) as f:
        f.write(contents)

def apply_prompt(p, x, xs):
    p.prompt = p.prompt.replace(xs[0], x)
    p.negative_prompt = p.negative_prompt.replace(xs[0], x)


def apply_order(p, x, xs):
    token_order = []

    # Initally grab the tokens from the prompt, so they can be replaced in order of earliest seen
    for token in x:
        token_order.append((p.prompt.find(token), token))

    token_order.sort(key=lambda t: t[0])

    prompt_parts = []

    # Split the prompt up, taking out the tokens
    for _, token in token_order:
        n = p.prompt.find(token)
        prompt_parts.append(p.prompt[0:n])
        p.prompt = p.prompt[n + len(token):]

    # Rebuild the prompt with the tokens in the order we want
    prompt_tmp = ""
    for idx, part in enumerate(prompt_parts):
        prompt_tmp += part
        prompt_tmp += x[idx]
    p.prompt = prompt_tmp + p.prompt
    

def build_samplers_dict(p):
    samplers_dict = {}
    for i, sampler in enumerate(get_correct_sampler(p)):
        samplers_dict[sampler.name.lower()] = i
        for alias in sampler.aliases:
            samplers_dict[alias.lower()] = i
    return samplers_dict


def apply_sampler(p, x, xs):
    sampler_index = build_samplers_dict(p).get(x.lower(), None)
    if sampler_index is None:
        raise RuntimeError(f"Unknown sampler: {x}")

    p.sampler_index = sampler_index


def apply_checkpoint(p, x, xs):
    info = modules.sd_models.get_closet_checkpoint_match(x)
    assert info is not None, f'Checkpoint for {x} not found'
    modules.sd_models.reload_model_weights(shared.sd_model, info)


def apply_hypernetwork(p, x, xs):
    hypernetwork.load_hypernetwork(x)


def apply_clip_skip(p, x, xs):
    opts.data["CLIP_stop_at_last_layers"] = x

def format_value_add_label(p, opt, x):
    if type(x) == float:
        x = round(x, 8)

    return f"{opt.label}: {x}"

def format_value(p, opt, x):
    if type(x) == float:
        x = round(x, 8)
    return x

def format_value_join_list(p, opt, x):
    return ", ".join(x)

def do_nothing(p, x, xs):
    pass

def format_nothing(p, opt, x):
    return ""

def str_permutations(x):
    """dummy function for specifying it in AxisOption's type when you want to get a list of permutations"""
    return x

AxisOption = namedtuple("AxisOption", ["label", "type", "apply", "format_value"])
AxisOptionImg2Img = namedtuple("AxisOptionImg2Img", ["label", "type", "apply", "format_value"])

axis_options = [
    AxisOption("Nothing", str, do_nothing, format_nothing),
    AxisOption("Seed", int, apply_field("seed"), format_value_add_label),
    AxisOption("Var. seed", int, apply_field("subseed"), format_value_add_label),
    AxisOption("Var. strength", float, apply_field("subseed_strength"), format_value_add_label),
    AxisOption("Steps", int, apply_field("steps"), format_value_add_label),
    AxisOption("CFG Scale", float, apply_field("cfg_scale"), format_value_add_label),
    AxisOption("Prompt S/R", str, apply_prompt, format_value),
    AxisOption("Prompt order", str_permutations, apply_order, format_value_join_list),
    AxisOption("Sampler", str, apply_sampler, format_value),
    AxisOption("Checkpoint name", str, apply_checkpoint, format_value),
    AxisOption("Hypernetwork", str, apply_hypernetwork, format_value),
    AxisOption("Sigma Churn", float, apply_field("s_churn"), format_value_add_label),
    AxisOption("Sigma min", float, apply_field("s_tmin"), format_value_add_label),
    AxisOption("Sigma max", float, apply_field("s_tmax"), format_value_add_label),
    AxisOption("Sigma noise", float, apply_field("s_noise"), format_value_add_label),
    AxisOption("Eta", float, apply_field("eta"), format_value_add_label),
    AxisOption("Clip skip", int, apply_clip_skip, format_value_add_label),
    AxisOptionImg2Img("Denoising", float, apply_field("denoising_strength"), format_value_add_label),  # as it is now all AxisOptionImg2Img items must go after AxisOption ones
]

def draw_xyz_grid(p, xs, ys, zs, x_labels, y_labels, z_labels, cell, web_path):
    data = {
        'x_labels': [x for x in x_labels],
        'y_labels': [y for y in y_labels],
        'z_labels': [z for z in z_labels]
    }

    first_processed = None

    state.job_count = len(xs) * len(ys) * len(zs) * p.n_iter
          
    n = 0
    data_arr = []
    for iz, z in enumerate(zs):
        data_arr.append([])
        data_arr[iz] = []
        for iy, y in enumerate(ys):
            data_arr[iz].append([])
            data_arr[iz][iy] = []
            for ix, x in enumerate(xs):
                if state.interrupted:
                    return None

                n += 1
                
                state.job = f"{n} out of {len(xs) * len(ys) * len(zs)}"

                processed = cell(x, y, z)
                if first_processed is None:
                    first_processed = processed
                
                # Manually save image so we control the filename
                try:
                    images.save_image(processed.images[0], p.outpath_samples, "", forced_filename=f"image-{iz}-{iy}-{ix}")
                except Exception:
                    print(f"ERROR saving generated image to path: {p.outpath_samples}")

                data_arr[iz][iy].append({
                    'info': processed.info,
                    'width': processed.width,
                    'height': processed.height,
                    'imgpath': f"image-{iz}-{iy}-{ix}.png"
                })

    data['images'] = data_arr

    shutil.copy2('scripts/xyz_grid.template.html', os.path.join(web_path, 'index.html'))

    # Can't load JSON directly from a file locally? Just wrap it in a function and include it like a .js file.
    writefile(web_path, 'data.js', f"function xyzData() {{ return {json.dumps(data)}; }}")

    return first_processed


re_range = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\(([+-]\d+)\s*\))?\s*")
re_range_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\(([+-]\d+(?:.\d*)?)\s*\))?\s*")

re_range_count = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\[(\d+)\s*\])?\s*")
re_range_count_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\[(\d+(?:.\d*)?)\s*\])?\s*")

class Script(scripts.Script):
    def title(self):
        return "X/Y/Z plot"

    def ui(self, is_img2img):
        current_axis_options = [x for x in axis_options if type(x) == AxisOption or type(x) == AxisOptionImg2Img and is_img2img]

        with gr.Row():
            x_type = gr.Dropdown(label="X type", choices=[x.label for x in current_axis_options], value=current_axis_options[1].label, visible=False, type="index", elem_id="x_type")
            x_values = gr.Textbox(label="X values", visible=False, lines=1)

        with gr.Row():
            y_type = gr.Dropdown(label="Y type", choices=[x.label for x in current_axis_options], value=current_axis_options[4].label, visible=False, type="index", elem_id="y_type")
            y_values = gr.Textbox(label="Y values", visible=False, lines=1)
        
        with gr.Row():
            z_type = gr.Dropdown(label="Z type", choices=[x.label for x in current_axis_options], value=current_axis_options[7].label, visible=False, type="index", elem_id="z_type")
            z_values = gr.Textbox(label="Z values", visible=False, lines=1)
        
        no_fixed_seeds = gr.Checkbox(label='Keep -1 for seeds', value=False)

        return [x_type, x_values, y_type, y_values, z_type, z_values, no_fixed_seeds]

    def run(self, p, x_type, x_values, y_type, y_values, z_type, z_values, no_fixed_seeds):
        if not no_fixed_seeds:
            modules.processing.fix_seed(p)

        p.batch_size = 1
        CLIP_stop_at_last_layers = opts.CLIP_stop_at_last_layers
        
        # Do not auto-save images, we need them in a specific format and the image filename is not returned :(
        p.do_not_save_samples = True
        p.do_not_save_grid = True

        def process_axis(opt, vals):
            if opt.label == 'Nothing':
                return [0]

            valslist = [x.strip() for x in chain.from_iterable(csv.reader(StringIO(vals)))]

            if opt.type == int:
                valslist_ext = []

                for val in valslist:
                    m = re_range.fullmatch(val)
                    mc = re_range_count.fullmatch(val)
                    if m is not None:
                        start = int(m.group(1))
                        end = int(m.group(2))+1
                        step = int(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += list(range(start, end, step))
                    elif mc is not None:
                        start = int(mc.group(1))
                        end   = int(mc.group(2))
                        num   = int(mc.group(3)) if mc.group(3) is not None else 1
                        
                        valslist_ext += [int(x) for x in np.linspace(start=start, stop=end, num=num).tolist()]
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == float:
                valslist_ext = []

                for val in valslist:
                    m = re_range_float.fullmatch(val)
                    mc = re_range_count_float.fullmatch(val)
                    if m is not None:
                        start = float(m.group(1))
                        end = float(m.group(2))
                        step = float(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += np.arange(start, end + step, step).tolist()
                    elif mc is not None:
                        start = float(mc.group(1))
                        end   = float(mc.group(2))
                        num   = int(mc.group(3)) if mc.group(3) is not None else 1
                        
                        valslist_ext += np.linspace(start=start, stop=end, num=num).tolist()
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == str_permutations:
                valslist = list(permutations(valslist))

            valslist = [opt.type(x) for x in valslist]
            
            # Confirm options are valid before starting
            if opt.label == "Sampler":
                samplers_dict = build_samplers_dict(p)
                for sampler_val in valslist:
                    if sampler_val.lower() not in samplers_dict.keys():
                        print('')
                        print(f"ERROR: Sampler {sampler_val.lower()} not found.")
                        print('List of samplers:')
                        print(*samplers_dict.keys(), sep='\n')
                        print('')
                        raise ValueError()
            elif opt.label == "Checkpoint name":
                for ckpt_val in valslist:
                    if modules.sd_models.get_closet_checkpoint_match(ckpt_val) is None:
                        print('')
                        print(f"ERROR: Checkpoint {ckpt_val} not found. (Provide name without .ckpt, or hash.)")
                        print('List of valid checkpoints:')
                        print(*modules.sd_models.checkpoints_list.keys(), sep='\n')
                        print('')
                        raise ValueError()

            return valslist

        try:
            x_opt = axis_options[x_type + 1]
            xs = process_axis(x_opt, x_values)

            y_opt = axis_options[y_type + 1]
            ys = process_axis(y_opt, y_values)

            z_opt = axis_options[z_type + 1]
            zs = process_axis(z_opt, z_values)
        except ValueError:
            state.interrupted = True
            return None

        def fix_axis_seeds(axis_opt, axis_list):
            if axis_opt.label == 'Seed':
                return [int(random.randrange(4294967294)) if val is None or val == '' or val == -1 else val for val in axis_list]
            else:
                return axis_list

        if not no_fixed_seeds:
            xs = fix_axis_seeds(x_opt, xs)
            ys = fix_axis_seeds(y_opt, ys)
            zs = fix_axis_seeds(z_opt, zs)

        total_steps = p.steps * len(xs) * len(ys) * len(zs)

        print('')
        print(f"X/Y/Z plot will create {len(xs) * len(ys) * len(zs) * p.n_iter} images on a {len(xs)}x{len(ys)}x{len(zs)} grid. (Total steps to process: {total_steps * p.n_iter})")
        shared.total_tqdm.updateTotal(total_steps * p.n_iter)

        if not opts.outdir_samples and not opts.outdir_txt2img_samples:
            print(f"ERROR: X/Y/Z Plot script requires that the Output Samples directory setting be set.")
            return None

        base_outpath = opts.outdir_samples or opts.outdir_txt2img_samples
        web_path = os.path.join(base_outpath, "xyz")
        os.makedirs(web_path, exist_ok=True)
        web_n = images.get_next_sequence_number(web_path, "")
        web_path = os.path.join(web_path, f"{web_n:05}")
        p.outpath_samples = os.path.join(web_path, "images")

        def cell(x, y, z):
            pc = copy(p)
            x_opt.apply(pc, x, xs)
            y_opt.apply(pc, y, ys)
            z_opt.apply(pc, z, zs)

            return process_images(pc)

        processed = draw_xyz_grid(
            p,
            xs=xs,
            ys=ys,
            zs=zs,
            x_labels=[x_opt.format_value(p, x_opt, x) for x in xs],
            y_labels=[y_opt.format_value(p, y_opt, y) for y in ys],
            z_labels=[z_opt.format_value(p, z_opt, z) for z in zs],
            cell=cell,
            web_path=web_path
        )
        
        if processed is None or state.interrupted:
            print('')
            return None

        # restore checkpoint in case it was changed by axes
        modules.sd_models.reload_model_weights(shared.sd_model)

        hypernetwork.load_hypernetwork(opts.sd_hypernetwork)

        opts.data["CLIP_stop_at_last_layers"] = CLIP_stop_at_last_layers

        print('') # Fixes some console glitching

        return processed
