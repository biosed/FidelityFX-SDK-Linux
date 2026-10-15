#!/usr/bin/env python3
"""
ffx_sc_native.py -- Native Linux replacement for FidelityFX_SC.exe

Compiles GLSL shaders into SPIR-V permutation headers identical in format
to those produced by FidelityFX_SC.exe. Uses the system glslangValidator
and performs SPIR-V reflection by parsing the binary directly.

Usage:
    ffx_sc_native.py [OPTIONS] <InputShaderFile>
"""

import sys
import os
import re
import struct
import hashlib
import subprocess
import tempfile
import math
import shutil
from itertools import product
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


# ---------------------------------------------------------------------------
# SPIR-V reflection parser
# ---------------------------------------------------------------------------

# SPIR-V opcodes we care about
OP_NAME              = 5
OP_TYPE_POINTER      = 32
OP_TYPE_IMAGE        = 25
OP_TYPE_SAMPLED_IMAGE = 27
OP_TYPE_SAMPLER      = 26
OP_TYPE_STRUCT       = 30
OP_TYPE_ARRAY        = 28
OP_TYPE_RUNTIME_ARRAY = 29
OP_VARIABLE          = 59
OP_DECORATE          = 71
OP_MEMBER_DECORATE   = 72
OP_TYPE_ACCELERATION_STRUCTURE = 5341

# Decorations
DECORATION_BINDING        = 33
DECORATION_DESCRIPTOR_SET = 34
DECORATION_NON_WRITABLE   = 24

# Storage classes
STORAGE_UNIFORM_CONSTANT = 0
STORAGE_UNIFORM          = 2   # UBO
STORAGE_STORAGE_BUFFER   = 12  # SSBO


class SpirvResource:
    """A single descriptor binding extracted from SPIR-V."""
    __slots__ = ('name', 'binding', 'descriptor_set', 'count',
                 'storage_class', 'type_id', 'kind')

    def __init__(self):
        self.name = ''
        self.binding = 0
        self.descriptor_set = 0
        self.count = 1
        self.storage_class = -1
        self.type_id = 0
        self.kind = ''  # 'cbv', 'srv_tex', 'uav_tex', 'srv_buf', 'uav_buf',
                        # 'sampler', 'rt_as'


def parse_spirv_string(words, start):
    """Decode a null-terminated UTF-8 string from SPIR-V word stream."""
    byte_data = b''
    for w in words[start:]:
        byte_data += struct.pack('<I', w)
    return byte_data.split(b'\x00', 1)[0].decode('utf-8', errors='replace')


def reflect_spirv(spirv_bytes):
    """
    Parse a SPIR-V binary and return reflection data grouped into the
    seven resource categories that FidelityFX expects.

    Returns a dict with keys: 'cbv', 'srv_tex', 'uav_tex', 'srv_buf',
    'uav_buf', 'sampler', 'rt_as'.  Each value is a list of
    (name, binding, count, set) tuples.
    """
    if len(spirv_bytes) < 20:
        return _empty_reflection()

    words = list(struct.unpack_from(f'<{len(spirv_bytes) // 4}I', spirv_bytes))

    magic = words[0]
    if magic != 0x07230203:
        raise ValueError(f'Bad SPIR-V magic: 0x{magic:08x}')

    names = {}          # id -> name string
    decorations = {}    # id -> {decoration_enum: value}
    variables = {}      # result_id -> (type_id, storage_class)
    type_pointers = {}  # result_id -> (storage_class, pointed_type_id)
    type_info = {}      # id -> type descriptor tuple
    non_writable_member_structs = set()

    pos = 5  # skip header
    while pos < len(words):
        instr = words[pos]
        opcode = instr & 0xFFFF
        word_count = (instr >> 16) & 0xFFFF
        if word_count == 0:
            break
        operands = words[pos + 1: pos + word_count]

        if opcode == OP_NAME and len(operands) >= 2:
            target_id = operands[0]
            name_str = parse_spirv_string(operands, 1)
            if name_str:
                names[target_id] = name_str

        elif opcode == OP_DECORATE and len(operands) >= 2:
            target_id = operands[0]
            decoration = operands[1]
            value = operands[2] if len(operands) >= 3 else None
            decorations.setdefault(target_id, {})[decoration] = value

        elif opcode == OP_MEMBER_DECORATE and len(operands) >= 3:
            if operands[2] == DECORATION_NON_WRITABLE:
                non_writable_member_structs.add(operands[0])

        elif opcode == OP_VARIABLE and len(operands) >= 3:
            result_type = operands[0]
            result_id = operands[1]
            storage_class = operands[2]
            variables[result_id] = (result_type, storage_class)

        elif opcode == OP_TYPE_POINTER and len(operands) >= 3:
            result_id = operands[0]
            storage_class = operands[1]
            pointed_type = operands[2]
            type_pointers[result_id] = (storage_class, pointed_type)

        elif opcode == OP_TYPE_IMAGE and len(operands) >= 7:
            result_id = operands[0]
            dim = operands[2]
            # operands: result_id, sampled_type, dim, depth, arrayed, ms, sampled, format
            sampled = operands[6] if len(operands) > 6 else 1
            type_info[result_id] = ('image', dim, sampled)

        elif opcode == OP_TYPE_SAMPLED_IMAGE and len(operands) >= 2:
            result_id = operands[0]
            image_type = operands[1]
            type_info[result_id] = ('sampled_image', image_type)

        elif opcode == OP_TYPE_SAMPLER and len(operands) >= 1:
            result_id = operands[0]
            type_info[result_id] = ('sampler',)

        elif opcode == OP_TYPE_STRUCT and len(operands) >= 1:
            result_id = operands[0]
            type_info[result_id] = ('struct', operands[1:])

        elif opcode == OP_TYPE_ARRAY and len(operands) >= 3:
            result_id = operands[0]
            element_type = operands[1]
            length_id = operands[2]
            type_info[result_id] = ('array', element_type, length_id)

        elif opcode == OP_TYPE_RUNTIME_ARRAY and len(operands) >= 2:
            result_id = operands[0]
            element_type = operands[1]
            type_info[result_id] = ('runtime_array', element_type)

        elif opcode == OP_TYPE_ACCELERATION_STRUCTURE and len(operands) >= 1:
            result_id = operands[0]
            type_info[result_id] = ('acceleration_structure',)

        pos += word_count

    # Now classify each variable that has a Binding decoration.
    result = {
        'cbv': [], 'srv_tex': [], 'uav_tex': [],
        'srv_buf': [], 'uav_buf': [],
        'sampler': [], 'rt_as': []
    }

    # Collect OpConstant values for array lengths.
    # Re-scan for OpConstant.
    constants = {}
    pos = 5
    while pos < len(words):
        instr = words[pos]
        opcode = instr & 0xFFFF
        word_count = (instr >> 16) & 0xFFFF
        if word_count == 0:
            break
        operands = words[pos + 1: pos + word_count]
        if opcode == 43 and len(operands) >= 3:  # OpConstant
            constants[operands[1]] = operands[2]
        elif opcode == 50 and len(operands) >= 3:  # OpSpecConstant
            constants[operands[1]] = operands[2]
        pos += word_count

    def resolve_type(type_id):
        """Walk the type chain and return a classification string."""
        if type_id in type_info:
            info = type_info[type_id]
            if info[0] == 'sampler':
                return 'sampler'
            if info[0] == 'sampled_image':
                return 'sampled_image'
            if info[0] == 'image':
                # sampled: 1 = sampled (SRV), 2 = storage (UAV)
                sampled = info[2]
                if sampled == 2:
                    return 'storage_image'
                return 'sampled_image'
            if info[0] == 'struct':
                return 'struct'
            if info[0] == 'array':
                return resolve_type(info[1])
            if info[0] == 'runtime_array':
                return resolve_type(info[1])
            if info[0] == 'acceleration_structure':
                return 'acceleration_structure'
        return 'unknown'

    def get_array_count(type_id):
        """Get array element count, or 1 if not an array."""
        if type_id in type_info:
            info = type_info[type_id]
            if info[0] == 'array':
                length_id = info[2]
                return constants.get(length_id, 1)
            if info[0] == 'runtime_array':
                return 0
        return 1

    for var_id, (ptr_type_id, storage_class) in variables.items():
        decos = decorations.get(var_id, {})
        if DECORATION_BINDING not in decos:
            continue

        binding = decos[DECORATION_BINDING]
        desc_set = decos.get(DECORATION_DESCRIPTOR_SET, 0)
        name = names.get(var_id, '')
        non_writable = DECORATION_NON_WRITABLE in decos

        # Resolve the pointed-to type.
        pointed_type_id = None
        if ptr_type_id in type_pointers:
            _, pointed_type_id = type_pointers[ptr_type_id]

        if pointed_type_id is None:
            continue

        # Determine array count from the pointed-to type.
        count = get_array_count(pointed_type_id)
        # If pointed_type_id is an array, resolve to element type for classification.
        actual_type_id = pointed_type_id
        if pointed_type_id in type_info:
            info = type_info[pointed_type_id]
            if info[0] in ('array', 'runtime_array'):
                actual_type_id = info[1]

        type_class = resolve_type(actual_type_id)

        if type_class == 'acceleration_structure':
            result['rt_as'].append((name, binding, count if count > 0 else 1, desc_set))
        elif type_class == 'sampler':
            result['sampler'].append((name, binding, count if count > 0 else 1, desc_set))
        elif type_class == 'sampled_image':
            result['srv_tex'].append((name, binding, count if count > 0 else 1, desc_set))
        elif type_class == 'storage_image':
            result['uav_tex'].append((name, binding, count if count > 0 else 1, desc_set))
        elif storage_class == STORAGE_UNIFORM:
            # UBO = constant buffer
            result['cbv'].append((name, binding, count if count > 0 else 1, desc_set))
        elif storage_class == STORAGE_STORAGE_BUFFER:
            # SRV when any block member is NonWritable: SPIRV-Reflect's rule,
            # which FidelityFX_SC.exe classifies by.
            if non_writable or actual_type_id in non_writable_member_structs:
                result['srv_buf'].append((name, binding, count if count > 0 else 1, desc_set))
            else:
                result['uav_buf'].append((name, binding, count if count > 0 else 1, desc_set))
        elif storage_class == STORAGE_UNIFORM_CONSTANT:
            # Could be combined image sampler, sampler, texture, or accel struct.
            if type_class == 'sampler':
                result['sampler'].append((name, binding, count if count > 0 else 1, desc_set))
            elif type_class == 'storage_image':
                result['uav_tex'].append((name, binding, count if count > 0 else 1, desc_set))
            elif type_class == 'sampled_image':
                result['srv_tex'].append((name, binding, count if count > 0 else 1, desc_set))
            elif type_class == 'acceleration_structure':
                result['rt_as'].append((name, binding, count if count > 0 else 1, desc_set))
            # Otherwise skip unknown types.

    return result


def _empty_reflection():
    return {
        'cbv': [], 'srv_tex': [], 'uav_tex': [],
        'srv_buf': [], 'uav_buf': [],
        'sampler': [], 'rt_as': []
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class ParsedArgs:
    def __init__(self):
        self.output_dir = '.'
        self.name = ''
        self.entry_point = 'main'
        self.target_env = 'vulkan1.2'
        self.stage = 'comp'
        self.optimize = False
        self.reflection = False
        self.deps_format = ''       # 'gcc' or ''
        self.fixed_defines = []     # [('NAME', 'VALUE'), ...]
        self.perm_defines = []      # [('NAME', ['v1','v2',...]), ...]
        self.include_paths = []     # ['/path/...', ...]
        self.compiler_args = []     # Extra args to pass through
        self.input_file = ''


def parse_args(argv):
    args = ParsedArgs()
    i = 0
    while i < len(argv):
        a = argv[i]

        if a.startswith('-output='):
            args.output_dir = a[len('-output='):]
        elif a.startswith('-name='):
            args.name = a[len('-name='):]
        elif a.startswith('-compiler='):
            pass  # Ignored; we always use system glslangValidator.
        elif a == '-e' and i + 1 < len(argv):
            i += 1
            args.entry_point = argv[i]
        elif a == '--target-env' and i + 1 < len(argv):
            i += 1
            args.target_env = argv[i]
        elif a == '-S' and i + 1 < len(argv):
            i += 1
            args.stage = argv[i]
        elif a == '-T' and i + 1 < len(argv):
            i += 1  # Ignore HLSL profile.
        elif a == '-Os':
            args.optimize = True
        elif a == '-reflection':
            args.reflection = True
        elif a.startswith('-deps='):
            args.deps_format = a[len('-deps='):]
        elif a.startswith('-D'):
            # Collect the full define, which may span multiple argv entries
            # if the {values} contain spaces (unlikely but handle it).
            define_str = a[2:]
            if '{' in define_str and '}' not in define_str:
                while i + 1 < len(argv) and '}' not in define_str:
                    i += 1
                    define_str += argv[i]

            if '{' in define_str:
                # Permutation define: NAME={v1,v2,...}
                eq_pos = define_str.index('=')
                name = define_str[:eq_pos]
                brace_open = define_str.index('{')
                brace_close = define_str.index('}')
                values_str = define_str[brace_open + 1:brace_close]
                values = [v.strip() for v in values_str.split(',')]
                args.perm_defines.append((name, values))
            else:
                # Fixed define: NAME=VALUE or just NAME.
                # If shell brace-expanded {0,1} into separate args,
                # detect duplicate define names and merge as permutation.
                if '=' in define_str:
                    dname = define_str.split('=', 1)[0]
                    dval  = define_str.split('=', 1)[1]
                    merged = False
                    for idx, (pn, pv) in enumerate(args.perm_defines):
                        if pn == dname and dval not in pv:
                            args.perm_defines[idx] = (pn, pv + [dval])
                            merged = True
                            break
                    if not merged:
                        # Check if already in fixed_defines with different value
                        existing = [d for d in args.fixed_defines
                                    if d.startswith(dname + '=')]
                        if existing:
                            prev_val = existing[0].split('=', 1)[1]
                            args.fixed_defines.remove(existing[0])
                            args.perm_defines.append((dname, [prev_val, dval]))
                        else:
                            args.fixed_defines.append(define_str)
                else:
                    args.fixed_defines.append(define_str)
        elif a.startswith('-I'):
            path = a[2:]
            if not path and i + 1 < len(argv):
                i += 1
                path = argv[i]
            args.include_paths.append(path)
        elif a.startswith('-') and a not in ('-', ):
            # Unknown flag -- pass through as compiler arg.
            args.compiler_args.append(a)
            # Consume following non-flag arguments as parameters.
            while i + 1 < len(argv) and not argv[i + 1].startswith('-') and i + 1 != len(argv) - 1:
                i += 1
                args.compiler_args.append(argv[i])
        else:
            # Positional argument = input file (last one wins).
            args.input_file = a

        i += 1

    if not args.input_file:
        print('Error: no input shader file specified.', file=sys.stderr)
        sys.exit(1)

    if not args.name:
        args.name = Path(args.input_file).stem

    return args


# ---------------------------------------------------------------------------
# Include dependency scanner (for depfiles)
# ---------------------------------------------------------------------------

def scan_includes(filepath, include_paths, visited=None):
    """Recursively scan #include directives and return a set of absolute paths."""
    if visited is None:
        visited = set()

    filepath = os.path.abspath(filepath)
    if filepath in visited:
        return visited
    visited.add(filepath)

    try:
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                stripped = line.lstrip()
                if not stripped.startswith('#include'):
                    continue
                # Extract filename from #include "..." or #include <...>
                m = re.search(r'#include\s*[<"]([^>"]+)[>"]', stripped)
                if not m:
                    continue
                inc_name = m.group(1)

                # Try to resolve relative to include paths.
                resolved = None
                # First try relative to the file's own directory.
                candidate = os.path.join(os.path.dirname(filepath), inc_name)
                if os.path.isfile(candidate):
                    resolved = os.path.abspath(candidate)
                else:
                    for ip in include_paths:
                        candidate = os.path.join(ip, inc_name)
                        if os.path.isfile(candidate):
                            resolved = os.path.abspath(candidate)
                            break

                if resolved and resolved not in visited:
                    scan_includes(resolved, include_paths, visited)
    except FileNotFoundError:
        pass

    return visited


# ---------------------------------------------------------------------------
# Source scanning for permutation option usage (early duplicate detection)
# ---------------------------------------------------------------------------

def scan_source_for_defines(input_file, include_paths, perm_defines):
    """
    Scan the shader source + includes for references to each permutation
    define name. Returns a list of booleans parallel to perm_defines
    indicating whether each define was found in the source.
    """
    all_files = scan_includes(input_file, include_paths)
    found = [False] * len(perm_defines)

    for filepath in all_files:
        try:
            with open(filepath, 'r', errors='replace') as f:
                content = f.read()
        except FileNotFoundError:
            continue

        for idx, (def_name, _) in enumerate(perm_defines):
            if not found[idx] and def_name in content:
                found[idx] = True

    return found


# ---------------------------------------------------------------------------
# Permutation expansion
# ---------------------------------------------------------------------------

class PermOption:
    """One permutation dimension."""
    __slots__ = ('name', 'values', 'num_bits', 'is_numeric', 'found_in_shader')

    def __init__(self, name, values, found):
        self.name = name
        self.values = values
        self.is_numeric = all(v.lstrip('-').isdigit() for v in values if v != '-')
        self.num_bits = max(1, math.ceil(math.log2(len(values)))) if len(values) > 1 else 1
        self.found_in_shader = found


def expand_permutations(perm_options):
    """
    Yield (key, defines_list, identical_to) for each permutation.
    `key` is the bit-packed permutation index.
    `defines_list` is a list of '-DNAME=VALUE' strings.
    `identical_to` is either None or the key of a prior permutation
    that this one is guaranteed identical to (define not found in source).
    """
    if not perm_options:
        yield 0, [], None
        return

    ranges = [range(len(opt.values)) for opt in perm_options]

    for combo in product(*ranges):
        key = 0
        cur_bit = 0
        defines = []
        identical_to = None
        first_unseen_divergence = None

        for opt_idx, val_idx in enumerate(combo):
            opt = perm_options[opt_idx]
            val = opt.values[val_idx]

            if val != '-':
                if opt.is_numeric:
                    defines.append(f'-D{opt.name}={val}')
                else:
                    defines.append(f'-D{val}')

            # Track early-duplicate detection: if an option is not found in
            # the shader source, all its value variants produce identical
            # SPIR-V.  We mark non-first values as identical to the first.
            if not opt.found_in_shader and val_idx != 0:
                if identical_to is None:
                    # Compute the key that has val_idx=0 for this option.
                    base_key = key  # key so far does not include this option yet
                    # base_key already accumulated prior options correctly.
                    # The "identical" key has val_idx=0 for this dimension
                    # (which contributes 0 bits) and same values for all
                    # subsequent dimensions.  But since we process in order,
                    # and this is the first divergence, we need the key with
                    # val_idx=0 here.  We will compute it after the loop.
                    first_unseen_divergence = (opt_idx, cur_bit)

            key |= (val_idx << cur_bit)
            cur_bit += opt.num_bits

        if first_unseen_divergence is not None:
            opt_idx_div, bit_div = first_unseen_divergence
            # Zero out the bits for the divergent option.
            opt = perm_options[opt_idx_div]
            mask = ((1 << opt.num_bits) - 1) << bit_div
            identical_to = key & ~mask

        yield key, defines, identical_to


# ---------------------------------------------------------------------------
# Compilation + header generation
# ---------------------------------------------------------------------------

def compile_permutation(glslang, args, perm_defines_strs, tmpdir, perm_key):
    """
    Compile one permutation, return (spirv_bytes, md5_hash) or raise on error.
    """
    spv_path = os.path.join(tmpdir, f'{perm_key}.spv')

    cmd = [glslang]
    cmd += ['--target-env', args.target_env]
    cmd += ['-S', args.stage]
    cmd += ['--entry-point', args.entry_point]
    if args.optimize:
        cmd.append('-Os')
    cmd.append('-V')

    # Fixed defines (pass as -D<def>).
    for d in args.fixed_defines:
        cmd.append(f'-D{d}')

    # Permutation defines.
    for d in perm_defines_strs:
        cmd.append(d)

    # Include paths.
    for ip in args.include_paths:
        cmd.append(f'-I{ip}')

    # Extra compiler args.
    cmd += args.compiler_args

    cmd += ['-o', spv_path, args.input_file]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f'glslangValidator failed for permutation key={perm_key}:\n'
            f'{proc.stderr}\n{proc.stdout}\n'
            f'Command: {" ".join(cmd)}'
        )

    with open(spv_path, 'rb') as f:
        spirv_bytes = f.read()

    md5 = hashlib.md5(spirv_bytes).hexdigest()
    return spirv_bytes, md5


def write_resource_arrays(fp, perm_name, resources, type_tag):
    """Write the four parallel arrays (Names, Bindings, Counts, Sets) for one resource type."""
    if not resources:
        return

    fp.write(f'static const char* g_{perm_name}_{type_tag}ResourceNames[] = {{ ')
    for name, _, _, _ in resources:
        fp.write(f' "{name}",')
    fp.write(' };\n')

    fp.write(f'static const uint32_t g_{perm_name}_{type_tag}ResourceBindings[] = {{ ')
    for _, binding, _, _ in resources:
        fp.write(f' {binding},')
    fp.write(' };\n')

    fp.write(f'static const uint32_t g_{perm_name}_{type_tag}ResourceCounts[] = {{ ')
    for _, _, count, _ in resources:
        fp.write(f' {count},')
    fp.write(' };\n')

    fp.write(f'static const uint32_t g_{perm_name}_{type_tag}ResourceSets[] = {{ ')
    for _, _, _, desc_set in resources:
        fp.write(f' {desc_set},')
    fp.write(' };\n\n')


def write_per_permutation_header(output_dir, shader_name, md5_hash,
                                 spirv_bytes, reflection, do_reflection):
    """Write one <name>_<hash>.h file."""
    perm_name = f'{shader_name}_{md5_hash}'
    header_filename = f'{perm_name}.h'
    header_path = os.path.join(output_dir, header_filename)

    with open(header_path, 'wb') as fp:
        def w(s):
            fp.write(s.encode('utf-8'))

        w(f'// {perm_name}.h.\n')
        w('// Auto generated by FidelityFX-SC.\n\n')

        if do_reflection and reflection:
            write_resource_arrays_to_file(w, perm_name, reflection)

        # Write SPIR-V data.
        size = len(spirv_bytes)
        w(f'static const uint32_t g_{perm_name}_size = {size};\n\n')
        w(f'static const unsigned char g_{perm_name}_data[] = {{\n')

        for i, b in enumerate(spirv_bytes):
            if i == size - 1:
                w(f'0x{b:02x}')
            elif (i + 1) % 16 == 0:
                w(f'0x{b:02x},\n')
            else:
                w(f'0x{b:02x},')

        w('\n};\n\n')

    return header_filename


def write_resource_arrays_to_file(w, perm_name, reflection):
    """Write reflection arrays using a write-function w(str)."""
    type_tags = [
        ('cbv', 'CBV'),
        ('srv_tex', 'TextureSRV'),
        ('uav_tex', 'TextureUAV'),
        ('srv_buf', 'BufferSRV'),
        ('uav_buf', 'BufferUAV'),
        ('sampler', 'Sampler'),
        ('rt_as', 'RTAccelerationStructure'),
    ]
    for key, tag in type_tags:
        resources = reflection.get(key, [])
        if not resources:
            continue

        w(f'static const char* g_{perm_name}_{tag}ResourceNames[] = {{ ')
        for name, _, _, _ in resources:
            w(f' "{name}",')
        w(' };\n')

        w(f'static const uint32_t g_{perm_name}_{tag}ResourceBindings[] = {{ ')
        for _, binding, _, _ in resources:
            w(f' {binding},')
        w(' };\n')

        w(f'static const uint32_t g_{perm_name}_{tag}ResourceCounts[] = {{ ')
        for _, _, count, _ in resources:
            w(f' {count},')
        w(' };\n')

        w(f'static const uint32_t g_{perm_name}_{tag}ResourceSets[] = {{ ')
        for _, _, _, desc_set in resources:
            w(f' {desc_set},')
        w(' };\n\n')


def write_perm_info_reflection_fields(fp, perm_name, reflection):
    """
    Write the reflection field values for one entry in the PermutationInfo
    table (inline, comma-separated).
    """
    type_tags = [
        ('cbv', 'CBV'),
        ('srv_tex', 'TextureSRV'),
        ('uav_tex', 'TextureUAV'),
        ('srv_buf', 'BufferSRV'),
        ('uav_buf', 'BufferUAV'),
        ('sampler', 'Sampler'),
        ('rt_as', 'RTAccelerationStructure'),
    ]
    for key, tag in type_tags:
        resources = reflection.get(key, [])
        n = len(resources)
        if n == 0:
            fp.write('0, 0, 0, 0, 0, ')
        else:
            fp.write(
                f'{n}, '
                f'g_{perm_name}_{tag}ResourceNames, '
                f'g_{perm_name}_{tag}ResourceBindings, '
                f'g_{perm_name}_{tag}ResourceCounts, '
                f'g_{perm_name}_{tag}ResourceSets, '
            )


def write_permutations_header(output_dir, shader_name, perm_options,
                              unique_perms, key_to_index, do_reflection):
    """Write the main <name>_permutations.h file."""
    header_path = os.path.join(output_dir, f'{shader_name}_permutations.h')

    with open(header_path, 'w') as fp:
        # Includes.
        for uperm in unique_perms:
            fp.write(f'#include "{uperm["header_filename"]}"\n')
        fp.write('\n')

        # Enum types for non-numeric permutation options.
        for opt in perm_options:
            if not opt.is_numeric:
                enum_name = opt.name
                fp.write(f'typedef enum {enum_name} {{\n')
                for j, val in enumerate(opt.values):
                    upper_name = enum_name.upper()
                    upper_val = val.upper()
                    suffix = ',' if j < len(opt.values) - 1 else ''
                    fp.write(f'    OPT_{upper_name}_{upper_val} = {j}{suffix}\n')
                fp.write(f'}} {enum_name};\n\n')

        # Permutation key union.
        union_name = f'{shader_name}_PermutationKey'
        fp.write(f'typedef union {union_name} {{\n')
        fp.write('    struct {\n')
        for opt in perm_options:
            fp.write(f'        uint32_t {opt.name} : {opt.num_bits};\n')
        fp.write('    };\n')
        fp.write('    uint32_t index;\n')
        fp.write(f'}} {union_name};\n\n')

        # PermutationInfo struct.
        fp.write(f'typedef struct {shader_name}_PermutationInfo {{\n')
        fp.write('    const uint32_t       blobSize;\n')
        fp.write('    const unsigned char* blobData;\n')
        if do_reflection:
            fp.write('\n')
            fp.write('\n')
            fp.write('    const uint32_t  numConstantBuffers;\n')
            fp.write('    const char**    constantBufferNames;\n')
            fp.write('    const uint32_t* constantBufferBindings;\n')
            fp.write('    const uint32_t* constantBufferCounts;\n')
            fp.write('    const uint32_t* constantBufferSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numSRVTextures;\n')
            fp.write('    const char**    srvTextureNames;\n')
            fp.write('    const uint32_t* srvTextureBindings;\n')
            fp.write('    const uint32_t* srvTextureCounts;\n')
            fp.write('    const uint32_t* srvTextureSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numUAVTextures;\n')
            fp.write('    const char**    uavTextureNames;\n')
            fp.write('    const uint32_t* uavTextureBindings;\n')
            fp.write('    const uint32_t* uavTextureCounts;\n')
            fp.write('    const uint32_t* uavTextureSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numSRVBuffers;\n')
            fp.write('    const char**    srvBufferNames;\n')
            fp.write('    const uint32_t* srvBufferBindings;\n')
            fp.write('    const uint32_t* srvBufferCounts;\n')
            fp.write('    const uint32_t* srvBufferSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numUAVBuffers;\n')
            fp.write('    const char**    uavBufferNames;\n')
            fp.write('    const uint32_t* uavBufferBindings;\n')
            fp.write('    const uint32_t* uavBufferCounts;\n')
            fp.write('    const uint32_t* uavBufferSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numSamplers;\n')
            fp.write('    const char**    samplerNames;\n')
            fp.write('    const uint32_t* samplerBindings;\n')
            fp.write('    const uint32_t* samplerCounts;\n')
            fp.write('    const uint32_t* samplerSpaces;\n')
            fp.write('\n')
            fp.write('    const uint32_t  numRTAccelerationStructures;\n')
            fp.write('    const char**    rtAccelerationStructureNames;\n')
            fp.write('    const uint32_t* rtAccelerationStructureBindings;\n')
            fp.write('    const uint32_t* rtAccelerationStructureCounts;\n')
            fp.write('    const uint32_t* rtAccelerationStructureSpaces;\n')
        fp.write(f'}} {shader_name}_PermutationInfo;\n\n')

        # Indirection table.
        total_bits = sum(opt.num_bits for opt in perm_options)
        table_size = 1 << total_bits if perm_options else 1

        fp.write(f'static const uint32_t g_{shader_name}_IndirectionTable[] = {{\n')
        for i in range(table_size):
            idx = key_to_index.get(i, 0)
            fp.write(f'    {idx},\n')
        fp.write('};\n\n')

        # PermutationInfo array.
        if unique_perms:
            fp.write(f'static const {shader_name}_PermutationInfo '
                     f'g_{shader_name}_PermutationInfo[] = {{\n')
            for uperm in unique_perms:
                perm_name = f'{shader_name}_{uperm["hash"]}'
                fp.write(f'    {{ g_{perm_name}_size, g_{perm_name}_data, ')
                if do_reflection:
                    write_perm_info_reflection_fields(fp, perm_name, uperm['reflection'])
                fp.write('},\n')
            fp.write('};\n\n')


def write_depfile_gcc(output_dir, shader_name, input_file, include_paths):
    """Write a gcc-format .d dependency file."""
    perm_header = os.path.abspath(
        os.path.join(output_dir, f'{shader_name}_permutations.h'))
    depfile_path = perm_header + '.d'

    deps = scan_includes(input_file, include_paths)
    # Remove the input file itself from deps (it is implicitly a dependency
    # but the original tool only lists includes).
    abs_input = os.path.abspath(input_file)
    deps.discard(abs_input)

    with open(depfile_path, 'wb') as f:
        line = perm_header + ':'
        for dep in sorted(deps):
            line += ' ' + dep
        f.write(line.encode('utf-8'))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_glslang():
    """Locate glslangValidator on the system."""
    # Check common locations.
    which = shutil.which('glslangValidator')
    if which:
        return which
    # Try Vulkan SDK.
    vulkan_sdk = os.environ.get('VULKAN_SDK', '')
    if vulkan_sdk:
        candidate = os.path.join(vulkan_sdk, 'bin', 'glslangValidator')
        if os.path.isfile(candidate):
            return candidate
    print('Error: glslangValidator not found in PATH or VULKAN_SDK.',
          file=sys.stderr)
    sys.exit(1)


def main():
    args = parse_args(sys.argv[1:])
    glslang = find_glslang()

    # Ensure output directory exists.
    os.makedirs(args.output_dir, exist_ok=True)

    # Determine which permutation options are referenced in source.
    #
    # FIX 2026-08-26: the text scan is UNSOUND — it failed to traverse the
    # FFX include chain, marked most options "not found", and the early-
    # duplicate shortcut then mapped every value of those options to the
    # value-0 blob WITHOUT COMPILING. Net effect: nearly all FSR3 passes
    # shipped a single permutation compiled with every option = 0 (non-HDR,
    # display-res MVs, STANDARD-Z on a reverse-Z engine) regardless of the
    # flags the runtime requested — the root cause of the FSR3 quality
    # failures observed on reverse-Z renderers. Treat every option as referenced:
    # every permutation is actually compiled, and the post-compile md5 dedup
    # (which is sound) collapses genuinely identical blobs.
    found_flags = [True] * len(args.perm_defines)

    # Sort: options found in shader first (matches original stable_partition).
    indexed_options = list(enumerate(args.perm_defines))
    found_first = []
    not_found_last = []
    for orig_idx, (name, values) in indexed_options:
        if found_flags[orig_idx]:
            found_first.append((name, values, True))
        else:
            not_found_last.append((name, values, False))
    sorted_perm_defs = found_first + not_found_last

    perm_options = [
        PermOption(name, values, found)
        for name, values, found in sorted_perm_defs
    ]

    # Print shader filename (matches original behavior).
    shader_filename = os.path.basename(args.input_file)
    print(shader_filename)

    # Expand all permutations.
    all_perms = list(expand_permutations(perm_options))
    total_permutations = len(all_perms)

    predicted_duplicates = sum(1 for _, _, ident in all_perms if ident is not None)

    # Compile permutations (with deduplication).
    tmpdir = os.path.join(args.output_dir, f'{args.name}_temp')
    os.makedirs(tmpdir, exist_ok=True)

    # unique_perms: ordered list of unique compiled permutations.
    unique_perms = []       # [{'hash': md5, 'spirv': bytes, 'reflection': dict,
    #                          'header_filename': str}, ...]
    hash_to_index = {}      # md5 -> index in unique_perms
    key_to_index = {}       # perm_key -> index in unique_perms

    # Separate permutations that need compilation from predicted duplicates.
    to_compile = []
    deferred = []
    for key, defines, identical_to in all_perms:
        if identical_to is not None:
            deferred.append((key, defines, identical_to))
        else:
            to_compile.append((key, defines))

    # Compile all non-duplicate permutations (parallelized).
    num_threads = min(os.cpu_count() or 1, max(1, len(to_compile)))

    def do_compile(item):
        key, defines = item
        spirv_bytes, md5_hash = compile_permutation(
            glslang, args, defines, tmpdir, key)
        reflection = None
        if args.reflection:
            reflection = reflect_spirv(spirv_bytes)
        return key, spirv_bytes, md5_hash, reflection

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(do_compile, item): item for item in to_compile}
        for future in as_completed(futures):
            key, spirv_bytes, md5_hash, reflection = future.result()

            if md5_hash not in hash_to_index:
                idx = len(unique_perms)
                hash_to_index[md5_hash] = idx
                header_fn = write_per_permutation_header(
                    args.output_dir, args.name, md5_hash,
                    spirv_bytes, reflection, args.reflection)
                unique_perms.append({
                    'hash': md5_hash,
                    'header_filename': header_fn,
                    'reflection': reflection,
                })

            key_to_index[key] = hash_to_index[md5_hash]

    # Resolve deferred (predicted-duplicate) permutations.
    for key, defines, identical_to in deferred:
        if identical_to in key_to_index:
            key_to_index[key] = key_to_index[identical_to]
        else:
            # The "identical" key hasn't been compiled yet (shouldn't happen
            # with correct ordering, but handle gracefully by compiling).
            spirv_bytes, md5_hash = compile_permutation(
                glslang, args, defines, tmpdir, key)
            reflection = None
            if args.reflection:
                reflection = reflect_spirv(spirv_bytes)

            if md5_hash not in hash_to_index:
                idx = len(unique_perms)
                hash_to_index[md5_hash] = idx
                header_fn = write_per_permutation_header(
                    args.output_dir, args.name, md5_hash,
                    spirv_bytes, reflection, args.reflection)
                unique_perms.append({
                    'hash': md5_hash,
                    'header_filename': header_fn,
                    'reflection': reflection,
                })
            key_to_index[key] = hash_to_index[md5_hash]

    # Clean up temp directory.
    shutil.rmtree(tmpdir, ignore_errors=True)

    if not unique_perms:
        print('Error: no shader permutations generated.', file=sys.stderr)
        sys.exit(1)

    # Write the main permutations header.
    write_permutations_header(
        args.output_dir, args.name, perm_options,
        unique_perms, key_to_index, args.reflection)

    # Write depfile if requested.
    if args.deps_format == 'gcc':
        write_depfile_gcc(
            args.output_dir, args.name,
            args.input_file, args.include_paths)

    actual_duplicates = total_permutations - len(unique_perms)
    print(f'{shader_filename}: Processed {total_permutations} shader permutations, '
          f'found {actual_duplicates} duplicates ({predicted_duplicates} found early).')


if __name__ == '__main__':
    main()
