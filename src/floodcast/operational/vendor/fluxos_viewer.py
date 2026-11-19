# Vendored verbatim from FLUXOS_cpp supporting_scripts/2_Read_Outputs/output_supporting_lib/fluxos_viewer.py
# (commit 44dabb0, plus local edits shown by `git -C FLUXOS_cpp diff`). Do not edit here: update FLUXOS and re-copy.
#!/usr/bin/env python3
# Copyright 2026, Diogo Costa, diogo.costa@uevora.pt
# This file is part of the FLUXOS model.

# This program, FLUXOS, is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
FLUXOS KML Exporter
====================

Export FLUXOS simulation results as lightweight KML + PNG files for animated
visualization in Google Earth.

Each timestep produces one small transparent PNG raster (GroundOverlay) instead
of thousands of polygon placemarks, keeping files compact (<1 MB total).

Supports both regular (Cartesian) and triangular mesh output formats.

Usage:
    # Regular mesh
    python fluxos_viewer.py --results-dir ./Results --dem ./terrain.asc --utm-zone 10

    # Triangular mesh
    python fluxos_viewer.py --results-dir ./Results_tri --dem ./terrain.asc \\
        --mesh-type triangular --utm-zone 10

    # Custom variable and colour range
    python fluxos_viewer.py --results-dir ./Results --dem ./terrain.asc \\
        --utm-zone 10 --variable velocity --clim 0 1.5

Requirements:
    pip install numpy pyproj
"""

import argparse
import datetime
import glob
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import math
import zlib

import numpy as np

try:
    from pyproj import Proj
except ImportError:
    print("ERROR: pyproj is required.  Install with: pip install pyproj")
    sys.exit(1)

# Video export (optional -- needed only for --export-video)
try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _check_ffmpeg():
    """Return True if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


# ═══════════════════════════════════════════════════════════════════════════════
#  MINIMAL PNG WRITER  (no Pillow / PIL dependency)
# ═══════════════════════════════════════════════════════════════════════════════

def _write_png(rgba_array):
    """
    Encode an (H, W, 4) uint8 RGBA array as a PNG file in memory.

    Returns bytes of the PNG file.
    """
    h, w = rgba_array.shape[:2]

    def _chunk(chunk_type, data):
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    # Signature
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    ihdr = _chunk(b"IHDR", ihdr_data)

    # IDAT — raw image data with filter byte 0 (None) per row
    raw_rows = []
    for y in range(h):
        raw_rows.append(b"\x00")  # filter: None
        raw_rows.append(rgba_array[y].tobytes())
    raw = b"".join(raw_rows)
    compressed = zlib.compress(raw, 9)
    idat = _chunk(b"IDAT", compressed)

    # IEND
    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


def _build_legend_png(clim, variable, opacity=180, width=300, height=40):
    """
    Create a colour-bar legend as a PNG image (returned as bytes).
    """
    var_labels = {
        "h": "Water Depth [m]",
        "velocity": "Velocity [m/s]",
        "conc_SW": "Concentration [mg/L]",
    }
    # Gradient bar matching the blue->cyan->yellow->red ramp (no text rendering)
    vals = np.linspace(clim[0] + 1e-9, clim[1], width).reshape(1, width)
    vals = np.broadcast_to(vals, (height, width)).copy()
    rgba = _value_to_rgba(vals, clim[0], clim[1], opacity)
    return _write_png(rgba)


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA I/O
# ═══════════════════════════════════════════════════════════════════════════════

def read_dem_asc(filepath):
    """Read an ESRI ASCII Grid (.asc) DEM file."""
    meta = {}
    header_lines = 6
    with open(filepath, "r") as f:
        for _ in range(header_lines):
            line = f.readline().strip()
            parts = line.split()
            key = parts[0].lower()
            val = parts[1]
            if key == "ncols":
                meta["ncols"] = int(val)
            elif key == "nrows":
                meta["nrows"] = int(val)
            elif key == "xllcorner":
                meta["xllcorner"] = float(val)
            elif key == "yllcorner":
                meta["yllcorner"] = float(val)
            elif key == "cellsize":
                meta["cellsize"] = float(val)
            elif key == "nodata_value":
                meta["nodata"] = float(val)

    elevation = np.loadtxt(filepath, skiprows=header_lines)
    if elevation.shape != (meta["nrows"], meta["ncols"]):
        raise ValueError(
            f"DEM shape {elevation.shape} != header "
            f"({meta['nrows']}, {meta['ncols']})"
        )
    nodata = meta.get("nodata", -99999.0)
    elevation[elevation == nodata] = np.nan

    valid = elevation[~np.isnan(elevation)]
    print(f"  DEM loaded: {meta['ncols']}x{meta['nrows']} cells, "
          f"cellsize={meta['cellsize']}m")
    if len(valid) > 0:
        print(f"  Elevation range: {valid.min():.1f} - {valid.max():.1f} m")
    return elevation, meta


def read_regular_results(results_dir):
    """Read regular mesh .txt output files."""
    pattern = os.path.join(results_dir, "*.txt")
    files = glob.glob(pattern)
    timesteps = []
    for f in files:
        basename = os.path.splitext(os.path.basename(f))[0]
        try:
            t = float(basename)
            timesteps.append((t, f))
        except ValueError:
            continue
    timesteps.sort(key=lambda x: x[0])
    if not timesteps:
        print(f"  WARNING: No numeric .txt files in {results_dir}")
        return []
    print(f"  Found {len(timesteps)} regular-mesh timesteps")
    results = []
    for t, filepath in timesteps:
        try:
            data = np.genfromtxt(filepath, delimiter=",", skip_header=1)
            if data.ndim == 1:
                data = data.reshape(1, -1)
            results.append({"time": t, "data": data})
        except Exception as e:
            print(f"  WARNING: Could not read {filepath}: {e}")
    if results:
        print(f"  Loaded {len(results)} timesteps "
              f"({results[0]['time']:.0f}s - {results[-1]['time']:.0f}s)")
    return results


def read_triangular_results(results_dir):
    """Read triangular mesh .vtu output files (XML VTK format)."""
    pvd_files = glob.glob(os.path.join(results_dir, "*.pvd"))
    timesteps = []
    if pvd_files:
        pvd_path = pvd_files[0]
        print(f"  Reading time series from: {os.path.basename(pvd_path)}")
        tree = ET.parse(pvd_path)
        root = tree.getroot()
        for dataset in root.iter("DataSet"):
            t = float(dataset.get("timestep", 0))
            vtu_file = dataset.get("file", "")
            if not os.path.isabs(vtu_file):
                vtu_file = os.path.join(results_dir, vtu_file)
            timesteps.append((t, vtu_file))
    else:
        vtu_files = glob.glob(os.path.join(results_dir, "*.vtu"))
        for f in vtu_files:
            basename = os.path.splitext(os.path.basename(f))[0]
            try:
                t = float(basename)
                timesteps.append((t, f))
            except ValueError:
                continue
    timesteps.sort(key=lambda x: x[0])
    if not timesteps:
        print(f"  WARNING: No .vtu files in {results_dir}")
        return []
    print(f"  Found {len(timesteps)} triangular-mesh timesteps")
    results = []
    for t, filepath in timesteps:
        try:
            pts, cells, cdata = _parse_vtu(filepath)
            results.append({"time": t, "points": pts, "cells": cells,
                            "cell_data": cdata})
        except Exception as e:
            print(f"  WARNING: Could not read {filepath}: {e}")
    if results:
        print(f"  Loaded {len(results)} timesteps "
              f"({results[0]['time']:.0f}s - {results[-1]['time']:.0f}s)")
    return results


def _parse_vtu(filepath):
    """Minimal VTU (XML) parser -- no PyVista dependency."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    piece = root.find(".//Piece")
    n_points = int(piece.get("NumberOfPoints"))
    n_cells = int(piece.get("NumberOfCells"))

    pts_da = piece.find(".//Points/DataArray")
    pts_flat = np.fromstring(pts_da.text.strip(), sep=" ")
    points = pts_flat.reshape(n_points, 3)

    cells_section = piece.find(".//Cells")
    conn_da = offsets_da = None
    for da in cells_section.findall("DataArray"):
        name = da.get("Name", "")
        if name == "connectivity":
            conn_da = da
        elif name == "offsets":
            offsets_da = da
    conn = np.fromstring(conn_da.text.strip(), dtype=int, sep=" ")
    offsets = np.fromstring(offsets_da.text.strip(), dtype=int, sep=" ")

    triangles = []
    prev = 0
    for off in offsets:
        cell_verts = conn[prev:off]
        if len(cell_verts) == 3:
            triangles.append(tuple(cell_verts))
        prev = off

    cell_data = {}
    cd_section = piece.find(".//CellData")
    if cd_section is not None:
        for da in cd_section.findall("DataArray"):
            name = da.get("Name", "unknown")
            n_comp = int(da.get("NumberOfComponents", "1"))
            vals = np.fromstring(da.text.strip(), sep=" ")
            if n_comp == 1:
                cell_data[name] = vals[:n_cells]
            else:
                cell_data[name] = vals.reshape(n_cells, n_comp)
    return points, triangles, cell_data


# ═══════════════════════════════════════════════════════════════════════════════
#  COORDINATE TRANSFORM
# ═══════════════════════════════════════════════════════════════════════════════

def make_utm_to_latlon(utm_zone, northern=True):
    """Return a function (easting, northing) -> (lon, lat)."""
    p = Proj(proj="utm", zone=utm_zone, ellps="WGS84", datum="WGS84",
             south=not northern)

    def transform(x, y):
        lon, lat = p(x, y, inverse=True)
        return lon, lat

    # Vectorised version for arrays
    def transform_arrays(x_arr, y_arr):
        return p(x_arr, y_arr, inverse=True)

    transform.arrays = transform_arrays
    return transform


def make_crs_to_latlon(crs):
    """Return ``(easting, northing) -> (lon, lat)`` for an arbitrary
    projected CRS — accepts any pyproj-parseable string (e.g.
    ``"EPSG:3763"`` for Portugal TM06, ``"EPSG:25830"`` for ETRS89
    UTM 30N, or a full PROJ string). Use this when the DEM is not in
    a plain WGS84 UTM zone."""
    from pyproj import Transformer
    tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def transform(x, y):
        lon, lat = tf.transform(x, y)
        return lon, lat

    def transform_arrays(x_arr, y_arr):
        return tf.transform(x_arr, y_arr)

    transform.arrays = transform_arrays
    return transform


def detect_utm_zone(xll, yll):
    """
    UTM zone cannot be uniquely determined from (easting, northing) alone.
    Defaults to zone 10 with a warning.
    """
    northern = yll < 10_000_000
    print("  WARNING: UTM zone cannot be auto-detected reliably from "
          "easting/northing alone.")
    print("           Defaulting to zone 10 (western Canada / US Pacific NW).")
    print("           Use --utm-zone to override if this is incorrect.")
    return 10, northern


# ═══════════════════════════════════════════════════════════════════════════════
#  RASTERIZE TO PNG
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_velocity_regular(data, meta):
    """Extract (vx, vy) from regular mesh data, with discharge fallback."""
    ux = data[:, 6]
    uy = data[:, 7]
    if np.any(ux != 0) or np.any(uy != 0):
        return ux, uy
    h = data[:, 5]
    cs = meta["cellsize"]
    qx_dxy = data[:, 8]
    qy_dxy = data[:, 9]
    vx = np.where(h > 1e-6, qx_dxy / (h * cs), 0.0)
    vy = np.where(h > 1e-6, qy_dxy / (h * cs), 0.0)
    return vx, vy


def _extract_velocity_triangular(cell_data):
    """Extract (vx, vy) from triangular mesh cell data, with discharge fallback."""
    if "velocity" in cell_data:
        vel = cell_data["velocity"]
        if vel.ndim == 2:
            vx, vy = vel[:, 0], vel[:, 1]
        else:
            vx, vy = vel, np.zeros_like(vel)
        if np.any(vx != 0) or np.any(vy != 0):
            return vx, vy
    if "discharge" in cell_data and "h" in cell_data:
        disc = cell_data["discharge"]
        h = cell_data["h"]
        if disc.ndim == 2:
            vx = np.where(h > 1e-6, disc[:, 0] / h, 0.0)
            vy = np.where(h > 1e-6, disc[:, 1] / h, 0.0)
            return vx, vy
    n = len(cell_data.get("h", []))
    return np.zeros(n), np.zeros(n)


def _value_to_rgba(values, vmin, vmax, opacity=180):
    """
    Vectorised: map an array of scalar values to RGBA (H, W, 4) or (N, 4).
    Transparent where value <= 0 or NaN.
    """
    # Replace NaN with 0 before computing colours (masked out below)
    safe = np.where(np.isnan(values), 0.0, values)
    t = np.clip((safe - vmin) / max(vmax - vmin, 1e-12), 0.0, 1.0)
    r = (200 * (1 - t)).astype(np.uint8)
    g = (220 * (1 - t) + 50 * t).astype(np.uint8)
    b = (255 * (1 - t * 0.3)).astype(np.uint8)
    a = (opacity * (0.3 + 0.7 * t)).astype(np.uint8)

    # Transparent for dry / NaN cells
    mask = np.isnan(values) | (values <= 0)
    r[mask] = 0
    g[mask] = 0
    b[mask] = 0
    a[mask] = 0

    rgba = np.stack([r, g, b, a], axis=-1)
    return rgba


# ═══════════════════════════════════════════════════════════════════════════════
#  HILLSHADE (for video background)
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_hillshade(dem, cellsize, azimuth=315, altitude=45):
    """Compute a hillshade from a DEM.

    Returns an (H, W) uint8 grayscale image (0-255).
    NaN cells in the DEM are rendered as neutral gray (128).
    """
    filled = dem.copy()
    nan_mask = np.isnan(filled)
    if np.any(nan_mask):
        # Fill NaN with nearest valid value to avoid gradient edge artifacts
        from scipy.ndimage import distance_transform_edt
        try:
            idx = distance_transform_edt(nan_mask, return_distances=False,
                                          return_indices=True)
            filled = filled[tuple(idx)]
        except Exception:
            filled[nan_mask] = np.nanmean(dem)

    dy, dx = np.gradient(filled, cellsize)
    slope = np.arctan(np.sqrt(dx**2 + dy**2))
    aspect = np.arctan2(-dy, dx)

    az_rad = np.radians(azimuth)
    alt_rad = np.radians(altitude)

    hs = (np.sin(alt_rad) * np.cos(slope) +
          np.cos(alt_rad) * np.sin(slope) * np.cos(az_rad - aspect))
    hs = np.clip(hs, 0, 1)
    hs_uint8 = (hs * 255).astype(np.uint8)
    hs_uint8[nan_mask] = 128
    return hs_uint8


# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW VISUALIZATION UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _velocity_colormap(t):
    """Map normalised velocity t∈[0,1] to (R,G,B).  Warm ramp: white→yellow→orange→red."""
    t = max(0.0, min(1.0, t))
    # Piecewise linear through 5 stops
    stops = [
        (0.00, 255, 255, 255),
        (0.25, 255, 255, 100),
        (0.50, 255, 180,  50),
        (0.75, 255,  80,  20),
        (1.00, 200,  20,  20),
    ]
    for i in range(len(stops) - 1):
        t0, r0, g0, b0 = stops[i]
        t1, r1, g1, b1 = stops[i + 1]
        if t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return (int(r0 + (r1 - r0) * f),
                    int(g0 + (g1 - g0) * f),
                    int(b0 + (b1 - b0) * f))
    return (200, 20, 20)


def _bilinear_interpolate(grid, r, c):
    """Bilinear interpolation of a 2D grid at fractional (row, col)."""
    h, w = grid.shape
    if r < 0 or r >= h - 1 or c < 0 or c >= w - 1:
        # Clamp to nearest edge value for edge pixels
        ri = max(0, min(h - 1, int(round(r))))
        ci = max(0, min(w - 1, int(round(c))))
        return float(grid[ri, ci])
    r0 = int(math.floor(r))
    c0 = int(math.floor(c))
    r1 = min(r0 + 1, h - 1)
    c1 = min(c0 + 1, w - 1)
    fr = r - r0
    fc = c - c0
    val = (grid[r0, c0] * (1 - fr) * (1 - fc) +
           grid[r0, c1] * (1 - fr) * fc +
           grid[r1, c0] * fr * (1 - fc) +
           grid[r1, c1] * fr * fc)
    return float(val)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADVECTED PARTICLE TRAIL ENGINE  (earth.nullschool-style)
# ═══════════════════════════════════════════════════════════════════════════════

def _bilinear_interpolate_vec(grid, rows, cols):
    """Vectorized bilinear interpolation for arrays of (row, col) positions.

    grid : float64[H, W]
    rows, cols : float64[N]
    Returns : float64[N]
    """
    H, W = grid.shape
    r0 = np.clip(np.floor(rows).astype(np.intp), 0, H - 2)
    c0 = np.clip(np.floor(cols).astype(np.intp), 0, W - 2)
    r1 = r0 + 1
    c1 = c0 + 1
    fr = np.clip(rows - r0, 0.0, 1.0)
    fc = np.clip(cols - c0, 0.0, 1.0)
    return (grid[r0, c0] * (1.0 - fr) * (1.0 - fc) +
            grid[r0, c1] * (1.0 - fr) * fc +
            grid[r1, c0] * fr * (1.0 - fc) +
            grid[r1, c1] * fr * fc)


def _rk4_advect(grid_vx, grid_vy, rows, cols, dt):
    """Vectorized RK4 integration step for particle advection.

    Returns (new_rows, new_cols, speed) where speed = |v| at the new position.
    Velocity convention: dr = -vy (north = row-decreasing), dc = vx.
    """
    def vel_at(r, c):
        vx = _bilinear_interpolate_vec(grid_vx, r, c)
        vy = _bilinear_interpolate_vec(grid_vy, r, c)
        return -vy, vx  # (dr, dc) in pixel space

    k1r, k1c = vel_at(rows, cols)
    k2r, k2c = vel_at(rows + 0.5 * dt * k1r, cols + 0.5 * dt * k1c)
    k3r, k3c = vel_at(rows + 0.5 * dt * k2r, cols + 0.5 * dt * k2c)
    k4r, k4c = vel_at(rows + dt * k3r, cols + dt * k3c)

    new_rows = rows + (dt / 6.0) * (k1r + 2.0 * k2r + 2.0 * k3r + k4r)
    new_cols = cols + (dt / 6.0) * (k1c + 2.0 * k2c + 2.0 * k3c + k4c)

    vx_f = _bilinear_interpolate_vec(grid_vx, new_rows, new_cols)
    vy_f = _bilinear_interpolate_vec(grid_vy, new_rows, new_cols)
    speed = np.sqrt(vx_f ** 2 + vy_f ** 2)
    return new_rows, new_cols, speed


# ── Advected-mode colormap ────────────────────────────────────────────────────

_ADVECTED_STOPS = np.array([
    # t,     R,    G,    B
    [0.00,  15,   20,   80],   # deep navy
    [0.15,  30,   70,  170],   # blue
    [0.40,  60,  190,  255],   # cyan
    [0.70, 200,  255,  255],   # light cyan / near-white
    [0.90, 255,  255,  180],   # warm white
    [1.00, 255,  230,  80],    # golden yellow
], dtype=np.float64)


def _advected_speed_colormap(speeds, vel_scale):
    """Vectorized: map speed array -> float32[N, 3] RGB in [0, 1]."""
    t = np.clip(speeds / max(vel_scale, 1e-12), 0.0, 1.0)
    stops = _ADVECTED_STOPS
    ts = stops[:, 0]
    rgb = np.zeros((len(t), 3), dtype=np.float32)
    for i in range(len(ts) - 1):
        mask = (t >= ts[i]) & (t <= ts[i + 1])
        if not np.any(mask):
            continue
        frac = (t[mask] - ts[i]) / max(ts[i + 1] - ts[i], 1e-12)
        for ch in range(3):
            v0 = stops[i, 1 + ch] / 255.0
            v1 = stops[i + 1, 1 + ch] / 255.0
            rgb[mask, ch] = v0 + (v1 - v0) * frac
    return rgb


def _advected_speed_colormap_scalar(speed, vel_scale):
    """Scalar version for colorbar drawing. Returns (R, G, B) ints 0-255."""
    t = max(0.0, min(1.0, speed / max(vel_scale, 1e-12)))
    stops = _ADVECTED_STOPS
    ts = stops[:, 0]
    for i in range(len(ts) - 1):
        if t <= ts[i + 1]:
            frac = (t - ts[i]) / max(ts[i + 1] - ts[i], 1e-12)
            r = int(stops[i, 1] + (stops[i + 1, 1] - stops[i, 1]) * frac)
            g = int(stops[i, 2] + (stops[i + 1, 2] - stops[i, 2]) * frac)
            b = int(stops[i, 3] + (stops[i + 1, 3] - stops[i, 3]) * frac)
            return (r, g, b)
    return (int(stops[-1, 1]), int(stops[-1, 2]), int(stops[-1, 3]))


# ── Velocity field temporal interpolation ─────────────────────────────────────

def _lerp_velocity_fields(vx0, vy0, vx1, vy1, frac):
    """Linear interpolation between two velocity fields."""
    return vx0 * (1.0 - frac) + vx1 * frac, vy0 * (1.0 - frac) + vy1 * frac


# ── Anti-aliased particle splatting ───────────────────────────────────────────

def _draw_particles_antialiased(canvas, rows, cols, colors, brightness):
    """Splat particles onto float32[H, W, 3] canvas with sub-pixel AA.

    canvas    : float32[H, W, 3]  -- modified in-place (values in [0, ~2])
    rows, cols: float64[N]
    colors    : float32[N, 3]     -- RGB in [0, 1]
    brightness: float64[N]        -- [0, 1]
    """
    H, W = canvas.shape[:2]
    r_int = np.floor(rows).astype(np.intp)
    c_int = np.floor(cols).astype(np.intp)
    fr = (rows - r_int).astype(np.float32)
    fc = (cols - c_int).astype(np.float32)

    # Bilinear weight distribution across 2×2 neighbourhood
    for dr in range(2):
        for dc in range(2):
            rr = r_int + dr
            cc = c_int + dc
            wr = fr if dr == 1 else (1.0 - fr)
            wc = fc if dc == 1 else (1.0 - fc)
            w = wr * wc * brightness.astype(np.float32)
            valid = (rr >= 0) & (rr < H) & (cc >= 0) & (cc < W)
            idx_r = rr[valid]
            idx_c = cc[valid]
            wv = w[valid]
            for ch in range(3):
                np.add.at(canvas[:, :, ch], (idx_r, idx_c),
                          colors[valid, ch] * wv)


# ── Particle System ───────────────────────────────────────────────────────────

class ParticleSystem:
    """Stateful particle system for earth.nullschool-style advection trails."""

    def __init__(self, n_particles, wet_mask, rng_seed=42):
        self.n = n_particles
        self.row = np.zeros(n_particles, dtype=np.float64)
        self.col = np.zeros(n_particles, dtype=np.float64)
        self.age = np.zeros(n_particles, dtype=np.int32)
        self.max_age = np.zeros(n_particles, dtype=np.int32)
        self.speed = np.zeros(n_particles, dtype=np.float64)
        self.stagnant_count = np.zeros(n_particles, dtype=np.int32)
        self.alive = np.zeros(n_particles, dtype=bool)
        self.rng = np.random.default_rng(rng_seed)
        self.wet_mask = wet_mask
        self._wet_indices = np.argwhere(wet_mask)
        self._spawn_at(np.arange(n_particles))

    def _spawn_at(self, indices):
        """Respawn specific particles at random wet cell locations."""
        n = len(indices)
        if len(self._wet_indices) == 0:
            self.alive[indices] = False
            return
        chosen = self.rng.choice(len(self._wet_indices), size=n,
                                  replace=(n > len(self._wet_indices)))
        base = self._wet_indices[chosen].astype(np.float64)
        base += self.rng.uniform(-0.4, 0.4, base.shape)
        self.row[indices] = base[:, 0]
        self.col[indices] = base[:, 1]
        self.age[indices] = 0
        self.max_age[indices] = self.rng.integers(60, 200, size=n)
        self.speed[indices] = 0.0
        self.stagnant_count[indices] = 0
        self.alive[indices] = True

    def advect(self, grid_vx, grid_vy, dt_pixels):
        """Advance all alive particles using RK4."""
        mask = self.alive
        if not np.any(mask):
            return
        idx = np.where(mask)[0]
        r = self.row[idx]
        c = self.col[idx]
        new_r, new_c, spd = _rk4_advect(grid_vx, grid_vy, r, c, dt_pixels)
        self.row[idx] = new_r
        self.col[idx] = new_c
        self.speed[idx] = spd
        self.age[idx] += 1

        H, W = grid_vx.shape
        # Kill: out of bounds
        oob = (new_r < 0) | (new_r >= H - 1) | (new_c < 0) | (new_c >= W - 1)
        # Kill: in dry cell
        r_int = np.clip(np.round(new_r).astype(np.intp), 0, H - 1)
        c_int = np.clip(np.round(new_c).astype(np.intp), 0, W - 1)
        dry = ~self.wet_mask[r_int, c_int]
        # Kill: exceeded max age
        old = self.age[idx] > self.max_age[idx]
        # Kill: stagnant
        slow = spd < 0.005
        self.stagnant_count[idx] = np.where(slow,
                                             self.stagnant_count[idx] + 1, 0)
        stagnant = self.stagnant_count[idx] > 15
        dead = oob | dry | old | stagnant
        self.alive[idx[dead]] = False

    def respawn_dead(self):
        """Respawn all dead particles at new random wet locations."""
        dead_idx = np.where(~self.alive)[0]
        if len(dead_idx) > 0:
            self._spawn_at(dead_idx)

    def update_wet_mask(self, new_mask):
        """Update wet mask (e.g., when flooding extent changes)."""
        self.wet_mask = new_mask
        self._wet_indices = np.argwhere(new_mask)

    def get_draw_data(self):
        """Return (rows, cols, speeds, ages, max_ages) for alive particles."""
        mask = self.alive
        return (self.row[mask], self.col[mask], self.speed[mask],
                self.age[mask], self.max_age[mask])


# ── Velocity Field Cache (sliding window) ────────────────────────────────────

class VelocityFieldCache:
    """Lazy-loading cache with a sliding window of velocity fields."""

    def __init__(self, results, mesh_type, dem, meta, h_min, scale):
        self.results = results
        self.mesh_type = mesh_type
        self.dem = dem
        self.meta = meta
        self.h_min = h_min
        self.scale = scale
        self._cache = {}        # idx -> (vx_hr, vy_hr, wet_hr)
        self._times = np.array([r["time"] for r in results], dtype=np.float64)
        self._nrows, self._ncols = dem.shape
        self._cs = meta["cellsize"]
        self._xll = meta["xllcorner"]
        self._yll = meta["yllcorner"]

    def get(self, idx):
        """Get upscaled velocity field for timestep index."""
        if idx not in self._cache:
            self._evict_if_full()
            self._load(idx)
        return self._cache[idx]

    def _load(self, idx):
        r = self.results[idx]
        if self.mesh_type == "regular":
            vx, vy, wet, _h, _conc = _build_velocity_grids_regular(
                self.dem, self.meta, r["data"], self.h_min)
        else:
            vx, vy, wet, _h, _conc = _build_velocity_grids_triangular(
                r["points"], r["cells"], r["cell_data"],
                self.h_min, self._nrows, self._ncols,
                self._xll, self._yll, self._cs)
        s = self.scale
        vx_hr = _upscale_velocity_grid(vx, s)
        vy_hr = _upscale_velocity_grid(vy, s)
        wet_hr = _upscale_velocity_grid(
            wet.astype(np.uint8), s).astype(bool)
        self._cache[idx] = (vx_hr, vy_hr, wet_hr)

    def _evict_if_full(self):
        while len(self._cache) > 4:
            oldest = min(self._cache.keys())
            del self._cache[oldest]

    def find_bracket(self, sim_time):
        """Return (idx_lo, idx_hi, frac) for temporal interpolation."""
        times = self._times
        if sim_time <= times[0]:
            return 0, 0, 0.0
        if sim_time >= times[-1]:
            n = len(times) - 1
            return n, n, 0.0
        idx = int(np.searchsorted(times, sim_time, side='right')) - 1
        idx = max(0, min(idx, len(times) - 2))
        t0, t1 = times[idx], times[idx + 1]
        frac = (sim_time - t0) / max(t1 - t0, 1e-12)
        return idx, idx + 1, float(frac)


def _build_velocity_grids_regular(dem, meta, data, h_min):
    """Build (grid_vx, grid_vy, wet_mask, grid_h, grid_conc) from regular mesh data."""
    nrows, ncols = dem.shape
    cs = meta["cellsize"]
    xll = meta["xllcorner"]
    yll = meta["yllcorner"]

    vx, vy = _extract_velocity_regular(data, meta)
    h_depth = data[:, 5]
    x_coords = data[:, 2]
    y_coords = data[:, 3]

    col_idx = np.round((x_coords - xll) / cs).astype(int)
    row_idx = np.round((nrows - 1) - (y_coords - yll) / cs).astype(int)
    col_idx = np.clip(col_idx, 0, ncols - 1)
    row_idx = np.clip(row_idx, 0, nrows - 1)

    grid_vx = np.zeros((nrows, ncols))
    grid_vy = np.zeros((nrows, ncols))
    grid_h = np.zeros((nrows, ncols))
    grid_conc = np.zeros((nrows, ncols))
    wet_mask = np.zeros((nrows, ncols), dtype=bool)
    wet = h_depth > h_min
    grid_vx[row_idx[wet], col_idx[wet]] = vx[wet]
    grid_vy[row_idx[wet], col_idx[wet]] = vy[wet]
    grid_h[row_idx[wet], col_idx[wet]] = h_depth[wet]
    wet_mask[row_idx[wet], col_idx[wet]] = True

    # Extract concentration (column 11) if available
    if data.shape[1] > 11:
        conc = data[:, 11]
        conc_valid = np.where(conc >= 0, conc, 0.0)  # -1 = inactive
        grid_conc[row_idx[wet], col_idx[wet]] = conc_valid[wet]
    return grid_vx, grid_vy, wet_mask, grid_h, grid_conc


def _build_velocity_grids_triangular(points, cells, cell_data, h_min,
                                      nrows, ncols, xll, yll, cs):
    """Build (grid_vx, grid_vy, wet_mask, grid_h, grid_conc) from triangular mesh data.

    Each triangle fills ALL the grid pixels that lie inside it (barycentric
    scan-fill), not just the pixel under its centroid. Centroid-only
    binning leaves visible holes when the trimesh is coarser than the
    grid — a 120 m triangle maps to one pixel on a 30 m grid, so adjacent
    triangles produce a sparse "hexagonal disconnected" look in the
    WebGL viewer even though the mesh itself is fully connected. Filling
    the whole footprint matches what ``rasterize_triangular`` does for
    the colour raster, so the velocity/depth grids and the depth
    colouring align pixel-by-pixel.
    """
    if "h" not in cell_data:
        return (np.zeros((nrows, ncols)), np.zeros((nrows, ncols)),
                np.zeros((nrows, ncols), dtype=bool),
                np.zeros((nrows, ncols)),
                np.zeros((nrows, ncols)))

    h = cell_data["h"]
    vx, vy = _extract_velocity_triangular(cell_data)
    conc_data = cell_data.get("conc_SW", np.zeros_like(h))
    n_cells = len(cells)

    grid_vx = np.zeros((nrows, ncols))
    grid_vy = np.zeros((nrows, ncols))
    grid_h = np.zeros((nrows, ncols))
    grid_conc = np.zeros((nrows, ncols))
    grid_cnt = np.zeros((nrows, ncols))

    pts = np.asarray(points, dtype=float)

    for ci in range(n_cells):
        if h[ci] <= h_min:
            continue
        i0, i1, i2 = cells[ci]
        p0, p1, p2 = pts[i0], pts[i1], pts[i2]

        # Triangle vertices in grid (col, row) coordinates.
        c0 = (p0[0] - xll) / cs
        c1 = (p1[0] - xll) / cs
        c2 = (p2[0] - xll) / cs
        r0 = (nrows - 1) - (p0[1] - yll) / cs
        r1 = (nrows - 1) - (p1[1] - yll) / cs
        r2 = (nrows - 1) - (p2[1] - yll) / cs

        # Bbox (inclusive), clipped to grid.
        r_min = max(0, int(np.floor(min(r0, r1, r2))))
        r_max = min(nrows - 1, int(np.ceil(max(r0, r1, r2))))
        c_min = max(0, int(np.floor(min(c0, c1, c2))))
        c_max = min(ncols - 1, int(np.ceil(max(c0, c1, c2))))
        if r_max < r_min or c_max < c_min:
            continue

        # Barycentric denominator.
        denom = (r1 - r2) * (c0 - c2) + (c2 - c1) * (r0 - r2)
        if abs(denom) < 1e-12:
            continue
        inv_denom = 1.0 / denom

        # Vectorised scan-fill of the bbox.
        rs = np.arange(r_min, r_max + 1, dtype=float)
        cs_grid = np.arange(c_min, c_max + 1, dtype=float)
        px, py = np.meshgrid(cs_grid, rs)  # px = col, py = row
        w0 = ((r1 - r2) * (px - c2) + (c2 - c1) * (py - r2)) * inv_denom
        w1 = ((r2 - r0) * (px - c2) + (c0 - c2) * (py - r2)) * inv_denom
        w2 = 1.0 - w0 - w1
        # Small epsilon so pixels right on an edge belong to ONE of the
        # two sharing triangles rather than being skipped by both.
        mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not np.any(mask):
            continue

        rr, cc = np.where(mask)
        rr_abs = rr + r_min
        cc_abs = cc + c_min
        grid_vx[rr_abs, cc_abs]  += vx[ci]
        grid_vy[rr_abs, cc_abs]  += vy[ci]
        grid_h[rr_abs, cc_abs]   += h[ci]
        c_val = conc_data[ci] if ci < len(conc_data) else 0.0
        grid_conc[rr_abs, cc_abs] += max(0.0, c_val)
        grid_cnt[rr_abs, cc_abs]  += 1

    valid = grid_cnt > 0
    grid_vx[valid] /= grid_cnt[valid]
    grid_vy[valid] /= grid_cnt[valid]
    grid_h[valid] /= grid_cnt[valid]
    grid_conc[valid] /= grid_cnt[valid]
    return grid_vx, grid_vy, valid, grid_h, grid_conc


def _draw_thick_line(img, r0, c0, r1, c1, rgba, width=1):
    """Draw a line with variable thickness using parallel Bresenham lines."""
    if width <= 1:
        _draw_line(img, r0, c0, r1, c1, rgba)
        return
    # Perpendicular direction
    dr = r1 - r0
    dc = c1 - c0
    length = math.sqrt(dr * dr + dc * dc)
    if length < 0.5:
        return
    # Normalised perpendicular
    pr = -dc / length
    pc = dr / length
    half = width / 2.0
    for i in range(width):
        offset = -half + 0.5 + i
        ro = int(round(offset * pr))
        co = int(round(offset * pc))
        _draw_line(img, r0 + ro, c0 + co, r1 + ro, c1 + co, rgba)


def _draw_segment(img, r0, c0, r1, c1, r_col, g_col, b_col, alpha, width=1):
    """Draw a coloured line segment with specified RGBA and width."""
    a = max(0, min(255, int(alpha)))
    if a < 5:
        return
    rgba = np.array([r_col, g_col, b_col, a], dtype=np.uint8)
    _draw_thick_line(img, int(round(r0)), int(round(c0)),
                     int(round(r1)), int(round(c1)), rgba, width)


def rasterize_regular(dem, meta, data, variable, clim, h_min, opacity):
    """
    Rasterize one regular-mesh timestep into an RGBA image (nrows x ncols x 4).
    """
    nrows, ncols = dem.shape
    cs = meta["cellsize"]
    xll = meta["xllcorner"]
    yll = meta["yllcorner"]

    x_coords = data[:, 2]
    y_coords = data[:, 3]
    h_depth = data[:, 5]

    if variable == "h":
        var_values = h_depth.copy()
    elif variable == "velocity":
        ux, uy = data[:, 6], data[:, 7]
        var_values = np.sqrt(ux**2 + uy**2)
    elif variable == "conc_SW":
        var_values = data[:, 11].copy()
    else:
        var_values = h_depth.copy()

    # Map scattered points to grid
    col_idx = np.round((x_coords - xll) / cs).astype(int)
    row_idx = np.round((nrows - 1) - (y_coords - yll) / cs).astype(int)
    col_idx = np.clip(col_idx, 0, ncols - 1)
    row_idx = np.clip(row_idx, 0, nrows - 1)

    grid = np.full((nrows, ncols), np.nan)
    wet = h_depth > h_min
    grid[row_idx[wet], col_idx[wet]] = var_values[wet]

    rgba = _value_to_rgba(grid, clim[0], clim[1], opacity)
    wet_count = int(np.sum(wet))
    return rgba.astype(np.uint8), wet_count


def rasterize_triangular(points, cells, cell_data, variable, clim,
                          h_min, opacity, nrows, ncols, xll, yll, cs):
    """
    Rasterize one triangular-mesh timestep into an RGBA image (nrows x ncols x 4).

    Uses vertex-based interpolation for smooth rendering:
    1. Average cell values to vertices (area-weighted)
    2. Rasterize each triangle using barycentric interpolation of vertex values
    3. This produces smooth gradients instead of flat per-cell shading.
    """
    if "h" not in cell_data:
        return np.zeros((nrows, ncols, 4), dtype=np.uint8), 0

    h = cell_data["h"]
    if variable == "h":
        var_values = h
    elif variable == "velocity":
        if "velocity" in cell_data:
            vel = cell_data["velocity"]
            if vel.ndim == 2:
                var_values = np.sqrt(vel[:, 0]**2 + vel[:, 1]**2)
            else:
                var_values = np.abs(vel)
        else:
            var_values = h
    elif variable == "conc_SW":
        var_values = cell_data.get("conc_SW", h)
    else:
        var_values = h

    # ── Step 1: Interpolate cell values to vertices (area-weighted average) ──
    npts = len(points)
    vertex_val_sum = np.zeros(npts)
    vertex_weight = np.zeros(npts)
    for ci, (i0, i1, i2) in enumerate(cells):
        if h[ci] <= h_min:
            continue
        val = var_values[ci]
        # Use 1.0 weight (uniform); could use cell area for area-weighting
        vertex_val_sum[i0] += val
        vertex_val_sum[i1] += val
        vertex_val_sum[i2] += val
        vertex_weight[i0] += 1.0
        vertex_weight[i1] += 1.0
        vertex_weight[i2] += 1.0

    # Vertex values: average of surrounding wet cells
    with np.errstate(invalid='ignore', divide='ignore'):
        vertex_vals = np.where(vertex_weight > 0, vertex_val_sum / vertex_weight, 0.0)
    vertex_wet = vertex_weight > 0

    # ── Step 2: Rasterize with barycentric interpolation ──
    grid = np.full((nrows, ncols), np.nan)
    wet_count = 0

    for ci, (i0, i1, i2) in enumerate(cells):
        if h[ci] <= h_min:
            continue
        wet_count += 1

        # Vertex values for this triangle
        v0_val = vertex_vals[i0]
        v1_val = vertex_vals[i1]
        v2_val = vertex_vals[i2]

        # Triangle vertices in grid coordinates
        p0, p1, p2 = points[i0], points[i1], points[i2]
        c0g = (p0[0] - xll) / cs
        c1g = (p1[0] - xll) / cs
        c2g = (p2[0] - xll) / cs
        r0g = (nrows - 1) - (p0[1] - yll) / cs
        r1g = (nrows - 1) - (p1[1] - yll) / cs
        r2g = (nrows - 1) - (p2[1] - yll) / cs

        # Bounding box of triangle
        r_min = max(0, int(np.floor(min(r0g, r1g, r2g))))
        r_max = min(nrows - 1, int(np.ceil(max(r0g, r1g, r2g))))
        c_min = max(0, int(np.floor(min(c0g, c1g, c2g))))
        c_max = min(ncols - 1, int(np.ceil(max(c0g, c1g, c2g))))

        # Precompute barycentric denominator
        denom = (r1g - r2g) * (c0g - c2g) + (c2g - c1g) * (r0g - r2g)
        if abs(denom) < 1e-12:
            continue  # Degenerate triangle

        inv_denom = 1.0 / denom

        # Scan-fill with barycentric interpolation
        for r in range(r_min, r_max + 1):
            for c in range(c_min, c_max + 1):
                px, py = float(c), float(r)
                # Barycentric coordinates
                w0 = ((r1g - r2g) * (px - c2g) + (c2g - c1g) * (py - r2g)) * inv_denom
                w1 = ((r2g - r0g) * (px - c2g) + (c0g - c2g) * (py - r2g)) * inv_denom
                w2 = 1.0 - w0 - w1

                if w0 >= -1e-6 and w1 >= -1e-6 and w2 >= -1e-6:
                    # Interpolated value
                    grid[r, c] = w0 * v0_val + w1 * v1_val + w2 * v2_val

    rgba = _value_to_rgba(grid, clim[0], clim[1], opacity)
    return rgba.astype(np.uint8), wet_count


# ═══════════════════════════════════════════════════════════════════════════════
#  VELOCITY ARROW OVERLAY
# ═══════════════════════════════════════════════════════════════════════════════

_ARROW_FG = np.array([255, 255, 255, 220], dtype=np.uint8)
_ARROW_BG = np.array([0, 0, 0, 160], dtype=np.uint8)


def _draw_arrow_outlined(img, r_from, c_from, r_to, c_to, head_size=3):
    """Draw an arrow with a dark outline for contrast."""
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        _draw_arrow(img, r_from + dr, c_from + dc,
                    r_to + dr, c_to + dc, _ARROW_BG, head_size)
    _draw_arrow(img, r_from, c_from, r_to, c_to, _ARROW_FG, head_size)


def draw_velocity_arrows_regular(img, dem, meta, data, h_min=0.001,
                                  arrow_spacing=15, max_arrow_px=12,
                                  vel_scale=None):
    """Overlay velocity arrows onto an RGBA image for regular mesh (in-place)."""
    nrows, ncols = dem.shape
    cs = meta["cellsize"]
    xll = meta["xllcorner"]
    yll = meta["yllcorner"]

    vx, vy = _extract_velocity_regular(data, meta)
    h_depth = data[:, 5]
    x_coords = data[:, 2]
    y_coords = data[:, 3]

    col_idx = np.round((x_coords - xll) / cs).astype(int)
    row_idx = np.round((nrows - 1) - (y_coords - yll) / cs).astype(int)
    col_idx = np.clip(col_idx, 0, ncols - 1)
    row_idx = np.clip(row_idx, 0, nrows - 1)

    grid_vx = np.zeros((nrows, ncols))
    grid_vy = np.zeros((nrows, ncols))
    wet = h_depth > h_min
    grid_vx[row_idx[wet], col_idx[wet]] = vx[wet]
    grid_vy[row_idx[wet], col_idx[wet]] = vy[wet]

    if vel_scale is None or vel_scale < 1e-9:
        vmag_all = np.sqrt(vx[wet] ** 2 + vy[wet] ** 2)
        valid = vmag_all[vmag_all > 1e-6]
        vel_scale = float(np.percentile(valid, 95)) if len(valid) > 0 else 1.0

    half = arrow_spacing // 2
    for r in range(half, nrows, arrow_spacing):
        for c in range(half, ncols, arrow_spacing):
            gvx = grid_vx[r, c]
            gvy = grid_vy[r, c]
            vmag = math.sqrt(gvx * gvx + gvy * gvy)
            if vmag < 1e-6:
                continue
            length_px = min(max_arrow_px, max_arrow_px * vmag / vel_scale)
            if length_px < 2:
                continue
            # vy positive = northward = pixel row decreasing
            dr = -gvy / vmag * length_px
            dc = gvx / vmag * length_px
            r_to = int(round(r + dr))
            c_to = int(round(c + dc))
            hs = max(2, int(length_px * 0.35))
            _draw_arrow_outlined(img, r, c, r_to, c_to, hs)


def draw_velocity_arrows_triangular(img, points, cells, cell_data, h_min,
                                     nrows, ncols, xll, yll, cs,
                                     arrow_spacing=15, max_arrow_px=12,
                                     vel_scale=None):
    """Overlay velocity arrows onto an RGBA image for triangular mesh (in-place)."""
    if "h" not in cell_data:
        return

    h = cell_data["h"]
    vx, vy = _extract_velocity_triangular(cell_data)

    # Compute cell centroids in pixel coordinates
    n_cells = len(cells)
    cx_px = np.zeros(n_cells)
    cy_px = np.zeros(n_cells)
    for ci, (i0, i1, i2) in enumerate(cells):
        p0, p1, p2 = points[i0], points[i1], points[i2]
        cx_utm = (p0[0] + p1[0] + p2[0]) / 3.0
        cy_utm = (p0[1] + p1[1] + p2[1]) / 3.0
        cx_px[ci] = (cx_utm - xll) / cs
        cy_px[ci] = (nrows - 1) - (cy_utm - yll) / cs

    wet = h > h_min

    if vel_scale is None or vel_scale < 1e-9:
        vmag_all = np.sqrt(vx[wet] ** 2 + vy[wet] ** 2)
        valid = vmag_all[vmag_all > 1e-6]
        vel_scale = float(np.percentile(valid, 95)) if len(valid) > 0 else 1.0

    # Bin cells into arrow_spacing blocks and average velocities
    n_bins_r = max(1, nrows // arrow_spacing + 1)
    n_bins_c = max(1, ncols // arrow_spacing + 1)
    bin_vx = np.zeros((n_bins_r, n_bins_c))
    bin_vy = np.zeros((n_bins_r, n_bins_c))
    bin_cnt = np.zeros((n_bins_r, n_bins_c))

    for ci in range(n_cells):
        if not wet[ci]:
            continue
        br = int(cy_px[ci] / arrow_spacing)
        bc = int(cx_px[ci] / arrow_spacing)
        if 0 <= br < n_bins_r and 0 <= bc < n_bins_c:
            bin_vx[br, bc] += vx[ci]
            bin_vy[br, bc] += vy[ci]
            bin_cnt[br, bc] += 1

    half = arrow_spacing // 2
    for br in range(n_bins_r):
        for bc in range(n_bins_c):
            if bin_cnt[br, bc] < 1:
                continue
            avg_vx = bin_vx[br, bc] / bin_cnt[br, bc]
            avg_vy = bin_vy[br, bc] / bin_cnt[br, bc]
            vmag = math.sqrt(avg_vx * avg_vx + avg_vy * avg_vy)
            if vmag < 1e-6:
                continue
            length_px = min(max_arrow_px, max_arrow_px * vmag / vel_scale)
            if length_px < 2:
                continue
            r_orig = br * arrow_spacing + half
            c_orig = bc * arrow_spacing + half
            dr = -avg_vy / vmag * length_px
            dc = avg_vx / vmag * length_px
            r_to = int(round(r_orig + dr))
            c_to = int(round(c_orig + dc))
            hs = max(2, int(length_px * 0.35))
            _draw_arrow_outlined(img, r_orig, c_orig, r_to, c_to, hs)


# ═══════════════════════════════════════════════════════════════════════════════
#  STREAMLINE VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def _integrate_streamline(grid_vx, grid_vy, r_start, c_start,
                           max_steps=20, step_size=1.5):
    """Euler integration of a streamline.  Returns [(r, c, vmag), ...]."""
    nrows, ncols = grid_vx.shape
    path = []
    r, c = float(r_start), float(c_start)
    for _ in range(max_steps):
        vx = _bilinear_interpolate(grid_vx, r, c)
        vy = _bilinear_interpolate(grid_vy, r, c)
        vmag = math.sqrt(vx * vx + vy * vy)
        if vmag < 1e-6:
            break
        path.append((r, c, vmag))
        # vy positive = northward = pixel row decreasing
        dr = -vy / vmag * step_size
        dc = vx / vmag * step_size
        r_new = r + dr
        c_new = c + dc
        if r_new < 0 or r_new >= nrows or c_new < 0 or c_new >= ncols:
            break
        r, c = r_new, c_new
    # Append final point
    if path:
        vx = _bilinear_interpolate(grid_vx, r, c)
        vy = _bilinear_interpolate(grid_vy, r, c)
        vmag = math.sqrt(vx * vx + vy * vy)
        path.append((r, c, vmag))
    return path


def _generate_seeds(wet_mask, density=15, jitter=True, seed=42):
    """Generate seed points on a regular grid across wet cells with optional jitter."""
    nrows, ncols = wet_mask.shape
    rng = np.random.default_rng(seed)
    seeds = []
    half = density // 2
    for row in range(half, nrows, density):
        for col in range(half, ncols, density):
            if wet_mask[row, col]:
                if jitter:
                    jr = rng.integers(-density // 4, density // 4 + 1)
                    jc = rng.integers(-density // 4, density // 4 + 1)
                    r = max(0, min(nrows - 1, row + jr))
                    c = max(0, min(ncols - 1, col + jc))
                    if wet_mask[r, c]:
                        seeds.append((float(r), float(c)))
                    else:
                        seeds.append((float(row), float(col)))
                else:
                    seeds.append((float(row), float(col)))
    return seeds


def _draw_streamline_path(img, path, vel_scale):
    """Draw a single streamline path with tapered opacity, velocity color, and outline."""
    n = len(path)
    if n < 2:
        return
    for i in range(n - 1):
        r0, c0, vmag0 = path[i]
        r1, c1, vmag1 = path[i + 1]
        vmag = (vmag0 + vmag1) * 0.5
        t = min(vmag / vel_scale, 1.0) if vel_scale > 1e-9 else 0.5
        # Taper: bright at head (i=0), fading toward tail
        progress = i / max(n - 1, 1)
        alpha = 220 * (1.0 - progress * 0.75)
        # Width: 1px slow → 3px fast
        w = 1 + int(t * 2)
        # Color
        rc, gc, bc = _velocity_colormap(t)
        # Dark outline
        outline_a = max(0, int(alpha * 0.6))
        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            _draw_segment(img, r0 + dr, c0 + dc, r1 + dr, c1 + dc,
                          0, 0, 0, outline_a, w)
        # Foreground
        _draw_segment(img, r0, c0, r1, c1, rc, gc, bc, alpha, w)


def draw_streamlines(img, grid_vx, grid_vy, wet_mask, vel_scale,
                      density=15, max_steps=20):
    """Draw streamlines onto an RGBA image (in-place)."""
    seeds = _generate_seeds(wet_mask, density)
    for r_seed, c_seed in seeds:
        path = _integrate_streamline(grid_vx, grid_vy, r_seed, c_seed,
                                      max_steps=max_steps, step_size=1.5)
        _draw_streamline_path(img, path, vel_scale)


# ═══════════════════════════════════════════════════════════════════════════════
#  PARTICLE TRACE VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_particle_positions(wet_mask, count=2000, seed=42):
    """Generate random particle positions across wet cells."""
    wet_idx = np.argwhere(wet_mask)
    if len(wet_idx) == 0:
        return np.zeros((0, 2))
    rng = np.random.default_rng(seed)
    n = min(count, len(wet_idx))
    chosen = rng.choice(len(wet_idx), size=n, replace=(count > len(wet_idx)))
    positions = wet_idx[chosen].astype(float)
    # Sub-pixel jitter
    positions += rng.uniform(-0.4, 0.4, positions.shape)
    return positions


def draw_particles(img, grid_vx, grid_vy, wet_mask, vel_scale,
                    count=2000, tail_length=8, frame_index=0):
    """Draw particle streaks onto an RGBA image (in-place).

    frame_index shifts the random seed so particles move between frames,
    creating an animation effect in the KML time slider.
    """
    nrows, ncols = grid_vx.shape
    seed = 1000 + frame_index * 7919
    positions = _generate_particle_positions(wet_mask, count, seed)
    if len(positions) == 0:
        return

    step_size = 0.8
    for pi in range(len(positions)):
        r, c = float(positions[pi, 0]), float(positions[pi, 1])
        trail = [(r, c)]
        for _ in range(tail_length):
            vx = _bilinear_interpolate(grid_vx, r, c)
            vy = _bilinear_interpolate(grid_vy, r, c)
            vmag = math.sqrt(vx * vx + vy * vy)
            if vmag < 1e-6:
                break
            dr = -vy / vmag * step_size
            dc = vx / vmag * step_size
            r_new = r + dr
            c_new = c + dc
            if r_new < 0 or r_new >= nrows or c_new < 0 or c_new >= ncols:
                break
            r, c = r_new, c_new
            trail.append((r, c))

        if len(trail) < 2:
            continue

        # Draw the streak: head is bright, tail fades
        for i in range(len(trail) - 1):
            r0, c0 = trail[i]
            r1, c1 = trail[i + 1]
            vx = _bilinear_interpolate(grid_vx, r0, c0)
            vy = _bilinear_interpolate(grid_vy, r0, c0)
            vmag = math.sqrt(vx * vx + vy * vy)
            t = min(vmag / vel_scale, 1.0) if vel_scale > 1e-9 else 0.5
            # Head (i=0) is bright, tail fades
            progress = i / max(len(trail) - 1, 1)
            alpha = 200 * (1.0 - progress * 0.8)
            rc, gc, bc = _velocity_colormap(t)
            _draw_segment(img, r0, c0, r1, c1, rc, gc, bc, alpha, 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW OVERLAY DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_flow_overlay(img, grid_vx, grid_vy, wet_mask, vel_scale,
                        flow_config, frame_index=0,
                        dem=None, meta=None, data=None,
                        points=None, cells=None, cell_data=None,
                        h_min=0.001, mesh_type="regular",
                        nrows=0, ncols=0, xll=0, yll=0, cs=1):
    """Dispatch to the appropriate flow visualization mode."""
    mode = flow_config.get("MODE", "arrows")

    if mode == "arrows":
        spacing = flow_config.get("ARROW_SPACING", 15)
        max_px = flow_config.get("ARROW_MAX_PX", 12)
        if mesh_type == "regular":
            draw_velocity_arrows_regular(img, dem, meta, data, h_min,
                                          spacing, max_px, vel_scale)
        else:
            draw_velocity_arrows_triangular(img, points, cells, cell_data,
                                             h_min, nrows, ncols, xll, yll, cs,
                                             spacing, max_px, vel_scale)

    elif mode == "streamlines":
        density = flow_config.get("STREAMLINE_DENSITY", 15)
        length = flow_config.get("STREAMLINE_LENGTH", 20)
        draw_streamlines(img, grid_vx, grid_vy, wet_mask, vel_scale,
                          density=density, max_steps=length)

    elif mode == "particles":
        count = flow_config.get("PARTICLE_COUNT", 2000)
        tail = flow_config.get("PARTICLE_TAIL", 8)
        draw_particles(img, grid_vx, grid_vy, wet_mask, vel_scale,
                        count=count, tail_length=tail, frame_index=frame_index)


# ═══════════════════════════════════════════════════════════════════════════════
#  GRID RASTERIZER  (mesh wireframe for the first frame)
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_line(img, r0, c0, r1, c1, rgba):
    """Bresenham's line into an (H, W, 4) image."""
    h, w = img.shape[:2]
    dr = abs(r1 - r0)
    dc = abs(c1 - c0)
    sr = 1 if r0 < r1 else -1
    sc = 1 if c0 < c1 else -1
    err = dc - dr
    while True:
        if 0 <= r0 < h and 0 <= c0 < w:
            img[r0, c0] = rgba
        if r0 == r1 and c0 == c1:
            break
        e2 = 2 * err
        if e2 > -dr:
            err -= dr
            c0 += sc
        if e2 < dc:
            err += dc
            r0 += sr


def _draw_arrow(img, r_from, c_from, r_to, c_to, rgba, head_size=3):
    """Draw an arrow (shaft + two barbs) onto an RGBA image."""
    dr = r_to - r_from
    dc = c_to - c_from
    length = math.sqrt(dr * dr + dc * dc)
    if length < 2:
        return
    _draw_line(img, r_from, c_from, r_to, c_to, rgba)
    angle = math.atan2(dr, dc)
    hs = max(2, int(head_size))
    for barb_offset in (5 * math.pi / 6, -5 * math.pi / 6):
        ba = angle + barb_offset
        rb = int(round(r_to + hs * math.sin(ba)))
        cb = int(round(c_to + hs * math.cos(ba)))
        _draw_line(img, r_to, c_to, rb, cb, rgba)


def rasterize_regular_grid(dem, meta, grid_color=(255, 255, 0, 160), spacing=1):
    """
    Draw the regular mesh grid lines onto a transparent RGBA image.
    Only draws lines where the DEM has valid (non-NaN) cells.
    """
    nrows, ncols = dem.shape
    img = np.zeros((nrows, ncols, 4), dtype=np.uint8)
    rgba = np.array(grid_color, dtype=np.uint8)

    # Mark domain outline and cell boundaries
    valid = ~np.isnan(dem)

    # Draw cell boundaries: highlight every Nth row and column
    step = max(1, spacing)
    # Horizontal lines
    for r in range(0, nrows, step):
        in_run = False
        for c in range(ncols):
            if valid[r, c]:
                if not in_run:
                    in_run = True
                img[r, c] = rgba
            else:
                in_run = False

    # Vertical lines
    for c in range(0, ncols, step):
        in_run = False
        for r in range(nrows):
            if valid[r, c]:
                if not in_run:
                    in_run = True
                img[r, c] = rgba
            else:
                in_run = False

    # Domain boundary: draw edges between valid and invalid
    boundary_rgba = np.array((255, 200, 0, 220), dtype=np.uint8)
    for r in range(nrows):
        for c in range(ncols):
            if not valid[r, c]:
                continue
            # Check 4-neighbours — if any is invalid or out-of-bounds, this is boundary
            is_edge = False
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = r + dr, c + dc
                if nr < 0 or nr >= nrows or nc < 0 or nc >= ncols:
                    is_edge = True
                elif np.isnan(dem[nr, nc]):
                    is_edge = True
            if is_edge:
                img[r, c] = boundary_rgba

    return img


def rasterize_triangular_grid(points, cells, nrows, ncols, xll, yll, cs,
                                grid_color=(255, 255, 0, 160)):
    """
    Draw the triangular mesh wireframe onto a transparent RGBA image.
    """
    img = np.zeros((nrows, ncols, 4), dtype=np.uint8)
    rgba = np.array(grid_color, dtype=np.uint8)

    for i0, i1, i2 in cells:
        p0, p1, p2 = points[i0], points[i1], points[i2]
        # Convert to pixel coordinates
        verts = []
        for p in [p0, p1, p2]:
            c = int(round((p[0] - xll) / cs))
            r = int(round((nrows - 1) - (p[1] - yll) / cs))
            verts.append((r, c))

        # Draw three edges
        _draw_line(img, verts[0][0], verts[0][1], verts[1][0], verts[1][1], rgba)
        _draw_line(img, verts[1][0], verts[1][1], verts[2][0], verts[2][1], rgba)
        _draw_line(img, verts[2][0], verts[2][1], verts[0][0], verts[0][1], rgba)

    return img


# ═══════════════════════════════════════════════════════════════════════════════
#  KML GENERATION  (one KML per timestep + master KML)
# ═══════════════════════════════════════════════════════════════════════════════

def format_time(seconds):
    """Format simulation time to readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f} min"
    elif seconds < 86400:
        return f"{seconds/3600:.1f} hr"
    else:
        return f"{seconds/86400:.1f} days"


def _ground_overlay_kml(png_filename, name, north, south, east, west,
                         ts_begin, ts_end):
    """Create a KML GroundOverlay referencing a PNG image file."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{name}</name>
  <GroundOverlay>
    <name>{name}</name>
    <TimeSpan>
      <begin>{ts_begin}</begin>
      <end>{ts_end}</end>
    </TimeSpan>
    <Icon>
      <href>{png_filename}</href>
    </Icon>
    <LatLonBox>
      <north>{north:.8f}</north>
      <south>{south:.8f}</south>
      <east>{east:.8f}</east>
      <west>{west:.8f}</west>
    </LatLonBox>
    <color>ffffffff</color>
  </GroundOverlay>
</Document>
</kml>
"""


def _master_kml(timestep_entries, title="FLUXOS Simulation",
                grid_kml_file=None):
    """
    Create a master KML that uses NetworkLinks to load per-timestep KML files.
    timestep_entries: list of (kml_filename, name, ts_begin, ts_end)
    grid_kml_file: optional always-visible grid overlay
    """
    grid_link = ""
    if grid_kml_file:
        grid_link = f"""  <NetworkLink>
    <name>Mesh Grid</name>
    <visibility>1</visibility>
    <Link>
      <href>{grid_kml_file}</href>
    </Link>
  </NetworkLink>
"""

    links = ""
    for kml_file, name, ts_begin, ts_end in timestep_entries:
        links += f"""  <NetworkLink>
    <name>{name}</name>
    <TimeSpan>
      <begin>{ts_begin}</begin>
      <end>{ts_end}</end>
    </TimeSpan>
    <Link>
      <href>{kml_file}</href>
    </Link>
  </NetworkLink>
"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{title}</name>
  <description>FLUXOS flood simulation results.
Use the time slider in Google Earth to animate.</description>
  <open>1</open>
{grid_link}{links}</Document>
</kml>
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _upscale_nearest(img, scale):
    """Upscale an (H, W, 4) RGBA image by integer factor using nearest neighbour."""
    if scale <= 1:
        return img
    return np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)


def _upscale_velocity_grid(grid, scale):
    """Upscale a 2D velocity grid by repeating each cell.

    Values are preserved (not interpolated) so that bilinear interpolation
    during streamline/particle integration operates on a finer pixel grid.
    """
    if scale <= 1:
        return grid
    return np.repeat(np.repeat(grid, scale, axis=0), scale, axis=1)


def _generate_frames(dem, meta, results, mesh_type, variable, clim,
                     opacity, h_min, draw_arrows, flow_config, vel_scale,
                     scale=1):
    """Yield (frame_index, time_seconds, rgba, wet_count) per timestep.

    The yielded rgba is an (H*scale, W*scale, 4) uint8 RGBA image
    with flow overlay already composited.  Dry frames are skipped.
    """
    nrows, ncols = dem.shape
    cs = meta["cellsize"]
    xll = meta["xllcorner"]
    yll = meta["yllcorner"]

    if flow_config is None:
        flow_config = {"MODE": "arrows", "ARROW_SPACING": 15, "ARROW_MAX_PX": 12,
                       "STREAMLINE_DENSITY": 15, "STREAMLINE_LENGTH": 20,
                       "PARTICLE_COUNT": 2000, "PARTICLE_TAIL": 8}

    for ri, r in enumerate(results):
        t_sec = r["time"]

        # Rasterize at native DEM resolution
        if mesh_type == "regular":
            rgba, wet = rasterize_regular(
                dem, meta, r["data"], variable, clim, h_min, opacity)
        else:
            rgba, wet = rasterize_triangular(
                r["points"], r["cells"], r["cell_data"],
                variable, clim, h_min, opacity, nrows, ncols, xll, yll, cs)

        if wet == 0:
            continue

        # Upscale depth colours to high-res canvas
        rgba = _upscale_nearest(rgba, scale)

        # Overlay flow visualisation (drawn at high-res)
        if draw_arrows and vel_scale is not None:
            if mesh_type == "regular":
                gvx, gvy, wmask, _h, _conc = _build_velocity_grids_regular(
                    dem, meta, r["data"], h_min)
            else:
                gvx, gvy, wmask, _h, _conc = _build_velocity_grids_triangular(
                    r["points"], r["cells"], r["cell_data"],
                    h_min, nrows, ncols, xll, yll, cs)
            gvx_hr = _upscale_velocity_grid(gvx, scale)
            gvy_hr = _upscale_velocity_grid(gvy, scale)
            wmask_hr = _upscale_velocity_grid(
                wmask.astype(np.uint8), scale).astype(bool)

            hr_flow_config = dict(flow_config)
            hr_flow_config["ARROW_SPACING"] = flow_config.get("ARROW_SPACING", 15) * scale
            hr_flow_config["ARROW_MAX_PX"] = flow_config.get("ARROW_MAX_PX", 12) * scale
            hr_flow_config["STREAMLINE_DENSITY"] = flow_config.get("STREAMLINE_DENSITY", 15) * scale
            hr_flow_config["STREAMLINE_LENGTH"] = flow_config.get("STREAMLINE_LENGTH", 20) * scale
            hr_flow_config["PARTICLE_TAIL"] = flow_config.get("PARTICLE_TAIL", 8) * scale
            hr_flow_config["PARTICLE_COUNT"] = flow_config.get("PARTICLE_COUNT", 2000) * scale

            _draw_flow_overlay(
                rgba, gvx_hr, gvy_hr, wmask_hr, vel_scale, hr_flow_config,
                frame_index=ri, dem=dem, meta=meta,
                data=r.get("data"), points=r.get("points"),
                cells=r.get("cells"), cell_data=r.get("cell_data"),
                h_min=h_min, mesh_type=mesh_type,
                nrows=nrows * scale, ncols=ncols * scale,
                xll=xll, yll=yll, cs=cs / scale)

        yield ri, t_sec, rgba, wet


def export_kml(dem, meta, results, mesh_type, variable, clim, utm_to_ll,
               sim_start, output_dir, opacity=180, h_min=0.001,
               draw_arrows=False, flow_config=None, scale=1):
    """
    Export results as individual KML + PNG files (one per timestep).

    scale: integer supersampling factor (1 = native DEM resolution,
           3 = 3× resolution in each dimension → 9× pixels).
    """
    nrows, ncols = dem.shape
    cs = meta["cellsize"]
    xll = meta["xllcorner"]
    yll = meta["yllcorner"]

    # Compute bounding box in lat/lon
    sw_lon, sw_lat = utm_to_ll(xll, yll)
    ne_lon, ne_lat = utm_to_ll(xll + ncols * cs, yll + nrows * cs)
    nw_lon, nw_lat = utm_to_ll(xll, yll + nrows * cs)
    se_lon, se_lat = utm_to_ll(xll + ncols * cs, yll)

    north = max(sw_lat, ne_lat, nw_lat, se_lat)
    south = min(sw_lat, ne_lat, nw_lat, se_lat)
    east = max(sw_lon, ne_lon, nw_lon, se_lon)
    west = min(sw_lon, ne_lon, nw_lon, se_lon)

    print(f"  Bounding box: lat [{south:.6f}, {north:.6f}], "
          f"lon [{west:.6f}, {east:.6f}]")

    os.makedirs(output_dir, exist_ok=True)

    timestep_entries = []
    total_size = 0

    # ── Frame 0: mesh grid ───────────────────────────────────
    if mesh_type == "regular":
        grid_step = max(1, min(nrows, ncols) // 60)
        grid_img = rasterize_regular_grid(dem, meta, spacing=grid_step)
    else:
        grid_img = rasterize_triangular_grid(
            results[0]["points"], results[0]["cells"],
            nrows, ncols, xll, yll, cs)

    grid_img = _upscale_nearest(grid_img, scale)

    grid_png_name = "grid.png"
    grid_png_path = os.path.join(output_dir, grid_png_name)
    grid_png_bytes = _write_png(grid_img)
    with open(grid_png_path, "wb") as f:
        f.write(grid_png_bytes)

    grid_kml_name = "grid.kml"
    grid_kml_path = os.path.join(output_dir, grid_kml_name)
    grid_kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>Mesh Grid</name>
  <GroundOverlay>
    <name>Mesh Grid ({mesh_type})</name>
    <Icon>
      <href>{grid_png_name}</href>
    </Icon>
    <LatLonBox>
      <north>{north:.8f}</north>
      <south>{south:.8f}</south>
      <east>{east:.8f}</east>
      <west>{west:.8f}</west>
    </LatLonBox>
    <color>ffffffff</color>
  </GroundOverlay>
</Document>
</kml>
"""
    with open(grid_kml_path, "w") as f:
        f.write(grid_kml_content)

    total_size += len(grid_png_bytes)
    out_h, out_w = grid_img.shape[:2]
    if scale > 1:
        print(f"  Grid frame: {mesh_type} mesh, {out_w}x{out_h}px ({scale}x upscale), "
              f"PNG {len(grid_png_bytes)/1024:.0f} KB")
    else:
        print(f"  Grid frame: {mesh_type} mesh, PNG {len(grid_png_bytes)/1024:.0f} KB")

    # Flow config defaults
    if flow_config is None:
        flow_config = {"MODE": "arrows", "ARROW_SPACING": 15, "ARROW_MAX_PX": 12,
                       "STREAMLINE_DENSITY": 15, "STREAMLINE_LENGTH": 20,
                       "PARTICLE_COUNT": 2000, "PARTICLE_TAIL": 8}

    # Pre-compute velocity scale
    vel_scale = None
    if draw_arrows:
        vel_scale = _auto_vel_scale(results, mesh_type, meta, h_min)
        mode = flow_config.get("MODE", "arrows")
        print(f"  Flow mode: {mode} (vel_scale={vel_scale:.4f} m/s)")

    # ── Generate frames using shared generator ────────────────
    n_total = len(results)
    for ri, t_sec, rgba, wet in _generate_frames(
            dem, meta, results, mesh_type, variable, clim,
            opacity, h_min, draw_arrows, flow_config, vel_scale, scale):

        # Time span
        t_start = sim_start + datetime.timedelta(seconds=t_sec)
        if ri + 1 < n_total:
            t_end = sim_start + datetime.timedelta(
                seconds=results[ri + 1]["time"])
        else:
            t_end = t_start + datetime.timedelta(seconds=t_sec * 0.5)

        ts_begin = t_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_end = t_end.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Write PNG
        png_name = f"frame_{ri:04d}.png"
        png_path = os.path.join(output_dir, png_name)
        png_bytes = _write_png(rgba)
        with open(png_path, "wb") as f:
            f.write(png_bytes)

        # Write per-timestep KML
        kml_name = f"frame_{ri:04d}.kml"
        kml_path = os.path.join(output_dir, kml_name)
        frame_label = f"t = {format_time(t_sec)}"
        kml_content = _ground_overlay_kml(
            png_name, frame_label, north, south, east, west,
            ts_begin, ts_end)
        with open(kml_path, "w") as f:
            f.write(kml_content)

        timestep_entries.append((kml_name, frame_label, ts_begin, ts_end))
        png_size = len(png_bytes)
        total_size += png_size

        print(f"  Frame {ri+1}/{n_total}: t={format_time(t_sec)}, "
              f"{wet} wet cells, PNG {png_size/1024:.0f} KB")

    # Write legend PNG
    legend_bytes = _build_legend_png(clim, variable, opacity)
    legend_path = os.path.join(output_dir, "legend.png")
    with open(legend_path, "wb") as f:
        f.write(legend_bytes)

    # Write master KML (with grid as always-visible layer)
    master_path = os.path.join(output_dir, "fluxos_animation.kml")
    master_kml = _master_kml(timestep_entries, f"FLUXOS {mesh_type.title()} Mesh",
                              grid_kml_file=grid_kml_name)
    with open(master_path, "w") as f:
        f.write(master_kml)

    n_frames = len(timestep_entries)
    print(f"\n  Exported {n_frames} frames to: {output_dir}/")
    print(f"  Total PNG size: {total_size/1024:.0f} KB")
    print(f"  Master KML: {master_path}")
    print(f"\n  Open in Google Earth:")
    print(f"    open {master_path}")
    print(f"  Then use the time slider to animate through timesteps.")


# ═══════════════════════════════════════════════════════════════════════════════
#  VIDEO EXPORT (GIF / MP4)
# ═══════════════════════════════════════════════════════════════════════════════

def _subsample_indices(n_total, max_frames):
    """Return evenly-spaced indices when n_total exceeds max_frames."""
    if n_total <= max_frames:
        return set(range(n_total))
    step = n_total / max_frames
    return set(int(round(i * step)) for i in range(max_frames))


def _composite_frame(hillshade_rgb, water_rgba):
    """Alpha-blend water RGBA over hillshade RGB background.

    Returns an (H, W, 3) uint8 RGB image.
    """
    alpha = water_rgba[:, :, 3:4].astype(np.float32) / 255.0
    water_rgb = water_rgba[:, :, :3].astype(np.float32)
    bg = hillshade_rgb.astype(np.float32)
    out = water_rgb * alpha + bg * (1.0 - alpha)
    return np.clip(out, 0, 255).astype(np.uint8)


def _get_font(size=20):
    """Get a TrueType font, falling back to default bitmap font."""
    if not _HAS_PIL:
        return None
    paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _draw_text_on_image(pil_img, text, x, y, font, bg_color=(0, 0, 0, 160),
                         fg_color=(255, 255, 255, 255)):
    """Draw text with a semi-transparent background rectangle."""
    draw = ImageDraw.Draw(pil_img, "RGBA")
    bbox = draw.textbbox((x, y), text, font=font)
    pad = 6
    draw.rectangle(
        [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad],
        fill=bg_color,
    )
    draw.text((x, y), text, fill=fg_color, font=font)


def _draw_colorbar(pil_img, clim, variable, width=200, height=18, margin=12):
    """Draw a horizontal colorbar in the bottom-right corner."""
    draw = ImageDraw.Draw(pil_img, "RGBA")
    img_w, img_h = pil_img.size
    font = _get_font(14)

    # Bar position
    x0 = img_w - width - margin
    y0 = img_h - height - margin - 20  # leave room for labels
    x1 = x0 + width
    y1 = y0 + height

    # Background panel
    draw.rectangle([x0 - 6, y0 - 22, x1 + 6, img_h - margin + 6],
                    fill=(0, 0, 0, 140))

    # Title
    var_label = {"h": "Water depth (m)", "velocity": "Velocity (m/s)",
                 "conc_SW": "Concentration"}.get(variable, variable)
    draw.text((x0, y0 - 18), var_label, fill=(255, 255, 255, 220), font=font)

    # Gradient bar (pixel by pixel along width)
    vmin, vmax = clim
    for px in range(width):
        t = px / max(width - 1, 1)
        val = vmin + t * (vmax - vmin)
        # Reuse same colour logic as _value_to_rgba
        tc = max(0.0, min(1.0, (val - vmin) / max(vmax - vmin, 1e-12)))
        r = int(200 * (1 - tc))
        g = int(220 * (1 - tc) + 50 * tc)
        b = int(255 * (1 - tc * 0.3))
        draw.line([(x0 + px, y0), (x0 + px, y1)], fill=(r, g, b, 220))

    # Min/max labels
    draw.text((x0, y1 + 2), f"{vmin:.2f}", fill=(255, 255, 255, 200),
              font=_get_font(12))
    draw.text((x1 - 30, y1 + 2), f"{vmax:.2f}", fill=(255, 255, 255, 200),
              font=_get_font(12))


def export_video(dem, meta, results, mesh_type, variable, clim,
                 output_path, fmt="gif", opacity=180, h_min=0.001,
                 draw_arrows=False, flow_config=None, scale=1,
                 fps=15, max_frames=200):
    """Export simulation results as an animated video (GIF or MP4).

    Parameters
    ----------
    fmt : str
        "gif" (Pillow, no external deps) or "mp4" (requires ffmpeg).
    scale : int
        Resolution multiplier (1 = native DEM resolution).
    fps : int
        Frames per second in the output video.
    max_frames : int
        Subsample if total frames exceed this.
    """
    if fmt == "gif" and not _HAS_PIL:
        print("ERROR: GIF export requires Pillow.  Install with: pip install Pillow")
        sys.exit(1)
    if fmt == "mp4" and not _check_ffmpeg():
        print("ERROR: MP4 export requires ffmpeg.  Install with:")
        print("  brew install ffmpeg    # macOS")
        print("  apt install ffmpeg     # Ubuntu/Debian")
        print("  Or use --export-video gif instead.")
        sys.exit(1)

    nrows, ncols = dem.shape
    cs = meta["cellsize"]

    # ── Hillshade background ──────────────────────────────────
    print("  Computing hillshade...")
    hs = _compute_hillshade(dem, cs)
    hs_up = _upscale_nearest(
        np.stack([hs, hs, hs, np.full_like(hs, 255)], axis=-1), scale)
    hillshade_rgb = hs_up[:, :, :3]  # (H*scale, W*scale, 3)
    out_h, out_w = hillshade_rgb.shape[:2]
    print(f"  Video resolution: {out_w}x{out_h} ({scale}x)")

    # ── Flow config defaults ──────────────────────────────────
    if flow_config is None:
        flow_config = {"MODE": "arrows", "ARROW_SPACING": 15, "ARROW_MAX_PX": 12,
                       "STREAMLINE_DENSITY": 15, "STREAMLINE_LENGTH": 20,
                       "PARTICLE_COUNT": 2000, "PARTICLE_TAIL": 8}

    vel_scale = None
    if draw_arrows:
        vel_scale = _auto_vel_scale(results, mesh_type, meta, h_min)
        mode = flow_config.get("MODE", "arrows")
        print(f"  Flow mode: {mode} (vel_scale={vel_scale:.4f} m/s)")

    # ── Subsample if too many frames ──────────────────────────
    n_total = len(results)
    keep = _subsample_indices(n_total, max_frames)
    print(f"  Processing {min(n_total, max_frames)} of {n_total} timesteps "
          f"(fps={fps})...")

    font = _get_font(max(16, int(out_h / 40)))

    # ── Generate and composite frames ─────────────────────────
    if fmt == "gif":
        pil_frames = []
        frame_count = 0
        for ri, t_sec, rgba, wet in _generate_frames(
                dem, meta, results, mesh_type, variable, clim,
                opacity, h_min, draw_arrows, flow_config, vel_scale, scale):

            if ri not in keep:
                continue

            # Composite over hillshade
            rgb = _composite_frame(hillshade_rgb, rgba)
            pil_img = Image.fromarray(rgb, "RGB")

            # Text overlay: timestamp
            _draw_text_on_image(pil_img, f"t = {format_time(t_sec)}",
                                12, 12, font)

            # Colorbar
            _draw_colorbar(pil_img, clim, variable,
                            width=min(200, out_w // 4), margin=12)

            # Quantize to 256 colours for GIF
            pil_frames.append(pil_img.quantize(colors=256,
                                                method=Image.Quantize.FASTOCTREE))

            frame_count += 1
            if frame_count % 20 == 0 or frame_count == 1:
                print(f"    Frame {frame_count}: t={format_time(t_sec)}, "
                      f"{wet} wet cells")

        if not pil_frames:
            print("  WARNING: No wet frames — video not created.")
            return

        print(f"  Writing GIF ({frame_count} frames, {fps} fps)...")
        duration_ms = int(1000 / fps)
        pil_frames[0].save(
            output_path,
            format="GIF",
            save_all=True,
            append_images=pil_frames[1:],
            duration=duration_ms,
            loop=0,
            optimize=False,
            disposal=2,
        )

        fsize = os.path.getsize(output_path)
        print(f"\n  Video saved: {output_path}")
        print(f"  Size: {fsize / 1024 / 1024:.1f} MB  |  "
              f"{frame_count} frames  |  {fps} fps  |  "
              f"{frame_count / fps:.1f}s duration")
        print(f"\n  Open with:  open {output_path}")

    elif fmt == "mp4":
        # Pipe raw RGB frames to ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{out_w}x{out_h}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "20",
            "-preset", "medium",
            output_path,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        frame_count = 0
        for ri, t_sec, rgba, wet in _generate_frames(
                dem, meta, results, mesh_type, variable, clim,
                opacity, h_min, draw_arrows, flow_config, vel_scale, scale):

            if ri not in keep:
                continue

            rgb = _composite_frame(hillshade_rgb, rgba)
            pil_img = Image.fromarray(rgb, "RGB")
            _draw_text_on_image(pil_img, f"t = {format_time(t_sec)}",
                                12, 12, font)
            _draw_colorbar(pil_img, clim, variable,
                            width=min(200, out_w // 4), margin=12)

            proc.stdin.write(np.array(pil_img).tobytes())
            frame_count += 1
            if frame_count % 20 == 0 or frame_count == 1:
                print(f"    Frame {frame_count}: t={format_time(t_sec)}, "
                      f"{wet} wet cells")

        proc.stdin.close()
        proc.wait()

        if proc.returncode != 0:
            print("  ERROR: ffmpeg exited with error.")
            sys.exit(1)

        fsize = os.path.getsize(output_path)
        print(f"\n  Video saved: {output_path}")
        print(f"  Size: {fsize / 1024 / 1024:.1f} MB  |  "
              f"{frame_count} frames  |  {fps} fps  |  "
              f"{frame_count / fps:.1f}s duration")
        print(f"\n  Play with:  open {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
#  ADVECTED PARTICLE TRAIL VIDEO EXPORT  (earth.nullschool style)
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_colorbar_advected(pil_img, vel_scale, width=200, height=18, margin=12):
    """Draw a speed colorbar (blue→cyan→white→yellow) for advected mode."""
    draw = ImageDraw.Draw(pil_img, "RGBA")
    img_w, img_h = pil_img.size
    font = _get_font(14)

    x0 = img_w - width - margin
    y0 = img_h - height - margin - 20
    x1 = x0 + width

    # Background panel
    draw.rectangle([x0 - 6, y0 - 22, x1 + 6, img_h - margin + 6],
                    fill=(0, 0, 0, 180))

    # Title
    draw.text((x0, y0 - 18), "Flow speed (m/s)",
              fill=(180, 210, 255, 220), font=font)

    # Gradient bar
    for px in range(width):
        t = px / max(width - 1, 1)
        speed = t * vel_scale
        r, g, b = _advected_speed_colormap_scalar(speed, vel_scale)
        draw.line([(x0 + px, y0), (x0 + px, y0 + height)],
                  fill=(r, g, b, 220))

    # Labels
    small_font = _get_font(12)
    draw.text((x0, y0 + height + 2), "0",
              fill=(180, 210, 255, 200), font=small_font)
    draw.text((x1 - 40, y0 + height + 2), f"{vel_scale:.2f}",
              fill=(180, 210, 255, 200), font=small_font)


def export_video_advected(dem, meta, results, mesh_type, variable, clim,
                          output_path, opacity=180, h_min=0.001,
                          scale=2, fps=30, max_frames=900,
                          n_particles=10000, fade_factor=0.96,
                          dark_factor=0.25):
    """Export earth.nullschool-style advected particle trail animation as MP4.

    Particles persist across frames, are advected through the velocity field
    via RK4 integration, and drawn onto a fade-texture canvas that produces
    smooth comet-like trails on a dark background.
    """
    if not _HAS_PIL:
        print("ERROR: Advected video export requires Pillow. "
              "Install with: pip install Pillow")
        sys.exit(1)
    if not _check_ffmpeg():
        print("ERROR: Advected video export requires ffmpeg. Install with:")
        print("  brew install ffmpeg    # macOS")
        print("  apt install ffmpeg     # Ubuntu/Debian")
        sys.exit(1)

    nrows, ncols = dem.shape
    cs = meta["cellsize"]

    # ── Hillshade background (dark) ──────────────────────────────
    print("  Computing dark hillshade background...")
    hs = _compute_hillshade(dem, cs)
    hs_rgba = np.stack([hs, hs, hs, np.full_like(hs, 255)], axis=-1)
    hs_up = _upscale_nearest(hs_rgba, scale)
    hillshade_rgb = hs_up[:, :, :3].astype(np.float32)
    # Darken and add a slight blue tint for cinematic look
    dark_hs = hillshade_rgb * dark_factor
    dark_hs[:, :, 2] = np.clip(dark_hs[:, :, 2] * 1.3, 0, 255)  # blue tint
    dark_hs = dark_hs.astype(np.uint8)
    out_h, out_w = dark_hs.shape[:2]
    print(f"  Video resolution: {out_w}x{out_h} ({scale}x)")

    # ── Velocity field cache ──────────────────────────────────────
    vel_cache = VelocityFieldCache(results, mesh_type, dem, meta, h_min, scale)
    vel_scale_val = _auto_vel_scale(results, mesh_type, meta, h_min)
    print(f"  Velocity scale: {vel_scale_val:.4f} m/s (95th percentile)")
    print(f"  Particles: {n_particles}, fade: {fade_factor}, dark: {dark_factor}")

    # ── Compute timing ────────────────────────────────────────────
    times = [r["time"] for r in results]
    sim_start_t = times[0]
    sim_end_t = times[-1]
    sim_duration = sim_end_t - sim_start_t
    if sim_duration <= 0:
        print("  ERROR: No time range in results.")
        return

    # Total video frames
    total_frames = min(max_frames, int(sim_duration / 10.0 * 3))  # ~3 frames per 10s step
    total_frames = max(total_frames, 60)  # at least 60 frames
    sim_dt_per_frame = sim_duration / total_frames  # seconds per video frame

    # Pixel size in metres
    pixel_size_m = cs / scale
    # dt_pixels: how many pixel-units a 1 m/s flow moves per video frame
    dt_pixels = sim_dt_per_frame / pixel_size_m
    # Sub-step if too large
    max_step = 1.5
    n_substeps = max(1, int(math.ceil(dt_pixels / max_step)))
    substep_dt = dt_pixels / n_substeps

    print(f"  Simulation: {format_time(sim_start_t)} → {format_time(sim_end_t)} "
          f"({len(results)} timesteps)")
    print(f"  Video: {total_frames} frames @ {fps} fps = "
          f"{total_frames / fps:.1f}s | {n_substeps} RK4 substeps/frame")

    # ── Initialize particle system ────────────────────────────────
    # Use wet mask from a mid-simulation timestep for initial spawn
    mid_idx = min(len(results) - 1, len(results) // 4)
    vx_init, vy_init, wet_init = vel_cache.get(mid_idx)
    particles = ParticleSystem(n_particles, wet_init, rng_seed=42)

    # ── Trail canvas ──────────────────────────────────────────────
    trail_canvas = np.zeros((out_h, out_w, 3), dtype=np.float32)

    # ── Open ffmpeg ───────────────────────────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{out_w}x{out_h}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "medium",
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    font = _get_font(max(16, int(out_h / 40)))

    print(f"  Rendering frames...")
    for frame_i in range(total_frames):
        sim_time = sim_start_t + frame_i * sim_dt_per_frame

        # (a) Get bracketing velocity fields
        idx_lo, idx_hi, frac = vel_cache.find_bracket(sim_time)
        vx_lo, vy_lo, wet_lo = vel_cache.get(idx_lo)
        vx_hi, vy_hi, wet_hi = vel_cache.get(idx_hi)

        # (b) Interpolate velocity field
        if idx_lo == idx_hi:
            vx_now, vy_now = vx_lo, vy_lo
            wet_now = wet_lo
        else:
            vx_now, vy_now = _lerp_velocity_fields(
                vx_lo, vy_lo, vx_hi, vy_hi, frac)
            wet_now = wet_lo | wet_hi

        # (c) Update particle wet mask
        particles.update_wet_mask(wet_now)

        # (d) Advect particles (sub-stepped RK4)
        for _ in range(n_substeps):
            particles.advect(vx_now, vy_now, substep_dt)

        # (e) Respawn dead particles
        particles.respawn_dead()

        # (f) Fade trail canvas
        trail_canvas *= fade_factor

        # (g) Draw particles onto trail canvas
        rows, cols, speeds, ages, max_ages = particles.get_draw_data()
        if len(rows) > 0:
            colors = _advected_speed_colormap(speeds, vel_scale_val)
            # Brightness: bright when young, fade near max_age
            age_frac = ages.astype(np.float64) / np.maximum(
                max_ages.astype(np.float64), 1.0)
            brightness = np.clip(1.0 - age_frac ** 1.5, 0.1, 1.0)
            _draw_particles_antialiased(
                trail_canvas, rows, cols, colors, brightness)

        # (h) Composite: dark hillshade + trail (additive)
        trail_rgb = np.clip(trail_canvas * 255.0, 0.0, 255.0)
        frame_rgb = np.clip(
            dark_hs.astype(np.float32) + trail_rgb, 0.0, 255.0
        ).astype(np.uint8)

        # (i) HUD overlays
        pil_img = Image.fromarray(frame_rgb, "RGB")
        _draw_text_on_image(pil_img, f"t = {format_time(sim_time)}",
                            12, 12, font)
        _draw_colorbar_advected(pil_img, vel_scale_val,
                                 width=min(200, out_w // 4), margin=12)

        # (j) Pipe to ffmpeg
        proc.stdin.write(np.array(pil_img).tobytes())

        if frame_i % 50 == 0 or frame_i == total_frames - 1:
            alive_count = int(np.sum(particles.alive))
            print(f"    Frame {frame_i + 1}/{total_frames}: "
                  f"t={format_time(sim_time)}, "
                  f"{alive_count} alive particles")

    proc.stdin.close()
    proc.wait()

    if proc.returncode != 0:
        print("  ERROR: ffmpeg exited with error.")
        sys.exit(1)

    fsize = os.path.getsize(output_path)
    print(f"\n  Video saved: {output_path}")
    print(f"  Size: {fsize / 1024 / 1024:.1f} MB  |  "
          f"{total_frames} frames  |  {fps} fps  |  "
          f"{total_frames / fps:.1f}s duration")
    print(f"\n  Play with:  open {output_path}")


# ═══════════════════════════════════════════════════════════════════════════════
#  SATELLITE IMAGERY DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def _download_satellite(sw_lat, sw_lon, ne_lat, ne_lon,
                        img_w, img_h, output_path,
                        basin_mask=None, sat_zoom_boost=0):
    """Download satellite imagery from Esri World Imagery and reproject to
    match a DEM grid.

    The DEM lives in UTM (locally equirectangular), so its pixel grid maps
    linearly to lat/lon over small areas.  Satellite tiles are in Web
    Mercator, where the Y-axis is nonlinear in latitude.  To align them we
    resample every output pixel individually: compute the lat/lon for that
    DEM cell, convert lat to Mercator-Y, and bilinearly sample the mosaic.

    If *basin_mask* is provided (a bool array, shape == (img_h, img_w),
    True = valid DEM cell), pixels outside the basin are darkened.
    """
    import urllib.request
    import io

    # Choose zoom level: aim for ~1 pixel per DEM cell. `sat_zoom_boost`
    # oversamples the source by 2^N (fetches 4^N more tiles) AND enlarges
    # the warped output by the same factor so the WebGL texture stays
    # crisp when the user zooms in on street details. Boost 2 ≈ zoom 19
    # with 4× linear resolution.
    lat_mid = (sw_lat + ne_lat) / 2.0
    lon_span = ne_lon - sw_lon
    meters_per_pixel_z0 = 156543.03392 * math.cos(math.radians(lat_mid))
    target_mpp = (lon_span * 111320 * math.cos(math.radians(lat_mid))) / img_w
    boost_scale = max(1, 2 ** int(sat_zoom_boost))
    if sat_zoom_boost:
        target_mpp = target_mpp / boost_scale
        img_w = int(img_w * boost_scale)
        img_h = int(img_h * boost_scale)
    zoom = max(1, min(19, int(round(math.log2(meters_per_pixel_z0 / target_mpp)))))
    if sat_zoom_boost:
        print(f"  Satellite: zoom level {zoom} "
              f"(boosted by {sat_zoom_boost}, output {img_w}×{img_h})")
    else:
        print(f"  Satellite: zoom level {zoom}")

    def lat_lon_to_tile(lat, lon, z):
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r))
                 / math.pi) / 2.0 * n)
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))

    def tile_bounds(tx, ty, z):
        """Return (west, south, east, north) in degrees for tile (tx, ty)."""
        n = 2 ** z
        west = tx / n * 360.0 - 180.0
        east = (tx + 1) / n * 360.0 - 180.0
        north = math.degrees(math.atan(math.sinh(
            math.pi * (1 - 2 * ty / n))))
        south = math.degrees(math.atan(math.sinh(
            math.pi * (1 - 2 * (ty + 1) / n))))
        return west, south, east, north

    tx_min, ty_min = lat_lon_to_tile(ne_lat, sw_lon, zoom)  # NW corner
    tx_max, ty_max = lat_lon_to_tile(sw_lat, ne_lon, zoom)  # SE corner

    n_tiles_x = tx_max - tx_min + 1
    n_tiles_y = ty_max - ty_min + 1
    print(f"  Satellite: {n_tiles_x}x{n_tiles_y} = "
          f"{n_tiles_x * n_tiles_y} tiles")

    # Download all tiles into a big mosaic (256 px per tile).
    #
    # Parallelised with a small thread pool — on a fresh cache with a
    # large domain (e.g. 9×8 = 72 tiles over 12×14 km) the sequential
    # path can stall for minutes, and a single bad tile with a 15 s
    # timeout blocks every subsequent fetch. Eight workers downloading
    # in parallel is enough to saturate a home connection without
    # tripping the ArcGIS rate limiter.
    #
    # On-disk tile cache lives alongside the output directory so
    # repeated exports over the same bbox (re-runs while iterating on
    # mesh/particles/trail length) don't re-fetch. Cache keyed on
    # zoom/x/y so it shares across domains that happen to overlap.
    tile_px = 256
    mosaic_w = n_tiles_x * tile_px
    mosaic_h = n_tiles_y * tile_px
    mosaic = np.full((mosaic_h, mosaic_w, 3), 128, dtype=np.uint8)

    url_template = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                    "World_Imagery/MapServer/tile/{z}/{y}/{x}")

    # Tile cache — under $HOME/.fluxos_tile_cache so it's not tied to
    # a specific export directory. Cheap disk for expensive downloads.
    cache_root = os.path.join(os.path.expanduser("~"), ".fluxos_tile_cache")
    try:
        os.makedirs(cache_root, exist_ok=True)
    except OSError:
        cache_root = None

    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None

    def _fetch_one(tx, ty):
        """Return (tx, ty, tile_rgb_array_or_None, cache_hit)."""
        cache_path = (os.path.join(cache_root, f"{zoom}_{tx}_{ty}.png")
                      if cache_root else None)
        if PILImage is None:
            return tx, ty, None, False
        # Cache hit?
        if cache_path and os.path.isfile(cache_path):
            try:
                arr = np.array(PILImage.open(cache_path).convert("RGB"))
                return tx, ty, arr, True
            except Exception:
                pass  # corrupt cache, refetch
        url = url_template.format(z=zoom, y=ty, x=tx)
        # Up to 3 attempts with short, increasing backoff. Overall cap
        # per tile ~ 15 s, so 72 tiles × 8 workers finishes in seconds
        # even if a couple of tiles fail.
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "FLUXOS-viewer/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    tile_data = resp.read()
                arr = np.array(PILImage.open(io.BytesIO(tile_data))
                               .convert("RGB"))
                if cache_path:
                    try:
                        with open(cache_path, "wb") as f:
                            f.write(tile_data)
                    except OSError:
                        pass
                return tx, ty, arr, False
            except Exception:
                if attempt == 2:
                    return tx, ty, None, False
                import time
                time.sleep(0.25 * (attempt + 1))
        return tx, ty, None, False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    tile_list = [(tx, ty) for ty in range(ty_min, ty_max + 1)
                          for tx in range(tx_min, tx_max + 1)]
    n_total = len(tile_list)
    n_done = 0
    n_failed = 0
    n_cached = 0
    # Keep the pool small so we don't hammer the tile server.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_one, tx, ty) for tx, ty in tile_list]
        for fut in as_completed(futures):
            tx, ty, arr, cache_hit = fut.result()
            n_done += 1
            if arr is None:
                n_failed += 1
            else:
                oy = (ty - ty_min) * tile_px
                ox = (tx - tx_min) * tile_px
                th, tw = arr.shape[:2]
                mosaic[oy:oy+th, ox:ox+tw] = arr[:, :, :3]
                if cache_hit:
                    n_cached += 1
            # Print progress every 10% (or every 5 tiles for small sets).
            every = max(1, n_total // 10, 5)
            if n_done % every == 0 or n_done == n_total:
                print(f"    tiles: {n_done}/{n_total} "
                      f"(cached {n_cached}, failed {n_failed})")
    if n_failed == n_total:
        print(f"    WARNING: all {n_total} tiles failed to download; "
              f"the satellite layer will be blank.")

    # ── Reproject: Mercator mosaic → equirectangular DEM grid ──
    # Compute the geographic bounds of the full tile mosaic
    w0, _, _, n0 = tile_bounds(tx_min, ty_min, zoom)
    _, s1, e1, _ = tile_bounds(tx_max, ty_max, zoom)
    mosaic_west, mosaic_east = w0, e1
    mosaic_north, mosaic_south = n0, s1

    def lat_to_merc_y(lat):
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

    merc_n = lat_to_merc_y(mosaic_north)
    merc_s = lat_to_merc_y(mosaic_south)
    merc_range = merc_n - merc_s
    lon_range = mosaic_east - mosaic_west

    # Build output image by sampling the Mercator mosaic per-pixel.
    # For each output row r (0=north=top), compute the latitude, convert
    # to Mercator-Y, and find the mosaic row. Longitude is linear.
    #
    # Vectorised with numpy — the original per-pixel Python loop over
    # 6+ M pixels (≈ 2375×2595 for a 12×14 km domain at 5× DEM) could
    # take minutes during which the terminal looked frozen. Now finishes
    # in a fraction of a second and progress is printed as we go through
    # the two steps (lat→merc conversion, bilinear sampling).
    print(f"    reprojecting mosaic ({mosaic_h}x{mosaic_w}px) "
          f"→ {img_h}x{img_w}px equirectangular …",
          flush=True)

    # Per-row mosaic Y (Mercator → row index), shape (img_h,)
    row_fracs = (np.arange(img_h) + 0.5) / img_h              # 0..1 N→S
    lats      = ne_lat - row_fracs * (ne_lat - sw_lat)         # (img_h,)
    merc_ys   = np.log(np.tan(np.pi / 4 + np.radians(lats) / 2))
    mosaic_rows_f = (merc_n - merc_ys) / merc_range * mosaic_h

    # Per-column mosaic X (longitude is linear; shape (img_w,))
    col_lons    = sw_lon + (np.arange(img_w) + 0.5) / img_w * (ne_lon - sw_lon)
    mosaic_cols = (col_lons - mosaic_west) / lon_range * mosaic_w

    # Broadcast to 2D (img_h × img_w) without materialising the full
    # grid twice: use outer-product-like indexing directly.
    R = mosaic_rows_f[:, None]          # (img_h, 1)
    C = mosaic_cols[None, :]            # (1, img_w)

    ry0 = np.clip(R.astype(np.int32), 0, mosaic_h - 2)   # (img_h, 1)
    cx0 = np.clip(C.astype(np.int32), 0, mosaic_w - 2)   # (1, img_w)
    ry1 = ry0 + 1
    cx1 = cx0 + 1
    fy  = R - ry0                       # (img_h, 1)
    fx  = C - cx0                       # (1, img_w)

    # Broadcast the integer indices to full (img_h, img_w) for fancy
    # indexing into mosaic (which is (mosaic_h, mosaic_w, 3)).
    RY0 = np.broadcast_to(ry0, (img_h, img_w))
    RY1 = np.broadcast_to(ry1, (img_h, img_w))
    CX0 = np.broadcast_to(cx0, (img_h, img_w))
    CX1 = np.broadcast_to(cx1, (img_h, img_w))

    p00 = mosaic[RY0, CX0].astype(np.float32)     # (img_h, img_w, 3)
    p10 = mosaic[RY0, CX1].astype(np.float32)
    p01 = mosaic[RY1, CX0].astype(np.float32)
    p11 = mosaic[RY1, CX1].astype(np.float32)

    wx = fx.astype(np.float32)                    # (1, img_w)
    wy = fy.astype(np.float32)                    # (img_h, 1)
    # Broadcast to (img_h, img_w, 1) for RGB multiply.
    wx3 = wx[..., None]
    wy3 = wy[..., None]
    val = (p00 * (1 - wx3) * (1 - wy3)
         + p10 *      wx3  * (1 - wy3)
         + p01 * (1 - wx3) *      wy3
         + p11 *      wx3  *      wy3)
    out = np.clip(val, 0.0, 255.0).astype(np.uint8)
    print("    reprojection done", flush=True)

    # ── Clip to basin (darken outside valid DEM cells) ─────────
    if basin_mask is not None:
        # After sat_zoom_boost the output is larger than the caller-supplied
        # basin_mask — nearest-neighbor up-sample the mask to match `out`.
        if basin_mask.shape != out.shape[:2]:
            sy = max(1, out.shape[0] // basin_mask.shape[0])
            sx = max(1, out.shape[1] // basin_mask.shape[1])
            if sy > 1 or sx > 1:
                basin_mask = np.repeat(np.repeat(basin_mask, sy, axis=0),
                                       sx, axis=1)
            # Trim any 1-2px rounding overshoot
            basin_mask = basin_mask[:out.shape[0], :out.shape[1]]
        if basin_mask.shape == out.shape[:2]:
            outside = ~basin_mask
            out[outside] = (out[outside].astype(np.float32) * 0.25).astype(
                np.uint8)

    # ── Save ───────────────────────────────────────────────────
    try:
        from PIL import Image as PILImage
        pil_img = PILImage.fromarray(out)
        pil_img.save(output_path, "JPEG", quality=85)
        fsize = os.path.getsize(output_path)
        print(f"  Satellite: {fsize/1024:.0f} KB "
              f"({img_w}x{img_h} px)")
    except ImportError:
        sat_rgba = np.stack([out[:, :, 0], out[:, :, 1], out[:, :, 2],
                             np.full(out.shape[:2], 255, np.uint8)], axis=-1)
        sat_bytes = _write_png(sat_rgba)
        out_png = output_path.replace(".jpg", ".png")
        with open(out_png, "wb") as f:
            f.write(sat_bytes)
        print(f"  Satellite (PNG fallback): {len(sat_bytes)/1024:.0f} KB")


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE WEBGL VIEWER EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

def export_webgl(dem, meta, results, mesh_type, variable, clim,
                 output_dir="fluxos_web", h_min=0.001, step=5,
                 n_particles=65536, utm_to_ll=None, sat_resolution=5,
                 sat_zoom_boost=0):
    """Export FLUXOS results as an interactive WebGL particle viewer.

    Creates a directory with index.html + data files that can be served
    via any HTTP server for an earth.nullschool-style visualization.
    """
    import json as _json

    nrows, ncols = dem.shape
    cs = meta["cellsize"]

    os.makedirs(output_dir, exist_ok=True)
    data_dir = os.path.join(output_dir, "data")
    vel_dir = os.path.join(data_dir, "velocity")
    os.makedirs(vel_dir, exist_ok=True)

    # ── Subsample timesteps ───────────────────────────────────
    indices = list(range(0, len(results), step))
    n_frames = len(indices)
    print(f"  Subsampling: {n_frames} of {len(results)} timesteps "
          f"(every {step}th)")

    # ── Pass 1: compute global velocity range + max depth ─────
    print("  Computing global velocity range + max depth...")
    all_vx = []
    all_vy = []
    all_depths = []
    all_concs = []
    has_concentration = False
    for idx in indices:
        r = results[idx]
        if mesh_type == "regular":
            gvx, gvy, wet, grid_h, grid_conc = _build_velocity_grids_regular(
                dem, meta, r["data"], h_min)
        else:
            gvx, gvy, wet, grid_h, grid_conc = _build_velocity_grids_triangular(
                r["points"], r["cells"], r["cell_data"],
                h_min, nrows, ncols,
                meta["xllcorner"], meta["yllcorner"], cs)
        # FLUXOS can leave NaN velocities in nearly-dry wet cells: zero them so the
        # ranges stay finite (metadata.json must be strict JSON) and the PNGs decode.
        gvx, gvy, grid_h, grid_conc = (np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
                                       for a in (gvx, gvy, grid_h, grid_conc))
        if np.any(wet):
            all_vx.append(gvx[wet])
            all_vy.append(gvy[wet])
            all_depths.append(grid_h[wet])
            conc_wet = grid_conc[wet]
            if np.any(conc_wet > 0):
                has_concentration = True
            all_concs.append(conc_wet)

    # Use 99.5th percentile for velocity range to clip outlier spikes
    if all_vx:
        cat_vx = np.concatenate(all_vx)
        cat_vy = np.concatenate(all_vy)
        gvx_min = float(np.percentile(cat_vx, 0.5))
        gvx_max = float(np.percentile(cat_vx, 99.5))
        gvy_min = float(np.percentile(cat_vy, 0.5))
        gvy_max = float(np.percentile(cat_vy, 99.5))
    else:
        gvx_min, gvx_max = -0.1, 0.1
        gvy_min, gvy_max = -0.1, 0.1

    # Ensure finite, non-zero range
    if not np.isfinite([gvx_min, gvx_max]).all() or gvx_max - gvx_min < 1e-9:
        gvx_min, gvx_max = -0.1, 0.1
    if not np.isfinite([gvy_min, gvy_max]).all() or gvy_max - gvy_min < 1e-9:
        gvy_min, gvy_max = -0.1, 0.1

    # Use 99.9th percentile for h_max to avoid outlier artefacts
    if all_depths:
        all_h = np.concatenate(all_depths)
        gh_max = float(np.percentile(all_h, 99.9))
    else:
        gh_max = 1.0
    if not np.isfinite(gh_max) or gh_max < 1e-6:
        gh_max = 1.0

    # Concentration max: use 95th percentile of significant values
    # The ADE solver produces very skewed distributions, so filter to
    # values above 1% of the overall max to focus on the actual plume.
    conc_max = 0.0
    if has_concentration and all_concs:
        cat_conc = np.concatenate(all_concs)
        cat_conc_pos = cat_conc[cat_conc > 0]
        if len(cat_conc_pos) > 0:
            raw_max = float(cat_conc_pos.max())
            # Keep only values above 1% of max (the actual plume)
            threshold = raw_max * 0.01
            plume = cat_conc_pos[cat_conc_pos >= threshold]
            if len(plume) > 0:
                conc_max = float(np.percentile(plume, 95))
            else:
                conc_max = raw_max
    if conc_max < 1e-6:
        conc_max = 1.0  # fallback

    print(f"  Velocity range: vx=[{gvx_min:.4f}, {gvx_max:.4f}], "
          f"vy=[{gvy_min:.4f}, {gvy_max:.4f}] m/s")
    print(f"  Max water depth: {gh_max:.4f} m")
    if has_concentration:
        print(f"  Max concentration: {conc_max:.4f} mg/L")

    # ── Pass 2: export velocity + depth + concentration PNGs ──
    print(f"  Exporting {n_frames} velocity+depth PNGs...")
    if has_concentration:
        conc_dir = os.path.join(data_dir, "conc")
        os.makedirs(conc_dir, exist_ok=True)
        print(f"  Also exporting {n_frames} concentration PNGs...")
    times = []
    total_bytes = 0
    for fi, idx in enumerate(indices):
        r = results[idx]
        times.append(float(r["time"]))

        if mesh_type == "regular":
            gvx, gvy, wet, grid_h, grid_conc = _build_velocity_grids_regular(
                dem, meta, r["data"], h_min)
        else:
            gvx, gvy, wet, grid_h, grid_conc = _build_velocity_grids_triangular(
                r["points"], r["cells"], r["cell_data"],
                h_min, nrows, ncols,
                meta["xllcorner"], meta["yllcorner"], cs)
        # FLUXOS can leave NaN velocities in nearly-dry wet cells: zero them so the
        # ranges stay finite (metadata.json must be strict JSON) and the PNGs decode.
        gvx, gvy, grid_h, grid_conc = (np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
                                       for a in (gvx, gvy, grid_h, grid_conc))

        # Encode vx → R, vy → G, depth → B, wet → A
        r_ch = ((gvx - gvx_min) / (gvx_max - gvx_min) * 255.0
                ).clip(0, 255).astype(np.uint8)
        g_ch = ((gvy - gvy_min) / (gvy_max - gvy_min) * 255.0
                ).clip(0, 255).astype(np.uint8)
        b_ch = (grid_h / gh_max * 255.0).clip(0, 255).astype(np.uint8)
        a_ch = np.where(wet, np.uint8(255), np.uint8(0))

        rgba = np.stack([r_ch, g_ch, b_ch, a_ch], axis=-1)
        png_bytes = _write_png(rgba)
        fname = f"v_{fi:04d}.png"
        with open(os.path.join(vel_dir, fname), "wb") as f:
            f.write(png_bytes)
        total_bytes += len(png_bytes)

        # Export concentration PNG: R=concentration, G=0, B=0, A=wet
        if has_concentration:
            c_r = (grid_conc / conc_max * 255.0).clip(0, 255).astype(np.uint8)
            c_g = np.zeros_like(c_r)
            c_b = np.zeros_like(c_r)
            c_a = np.where(wet & (grid_conc > 0), np.uint8(255), np.uint8(0))
            c_rgba = np.stack([c_r, c_g, c_b, c_a], axis=-1)
            c_bytes = _write_png(c_rgba)
            with open(os.path.join(conc_dir, f"c_{fi:04d}.png"), "wb") as f:
                f.write(c_bytes)
            total_bytes += len(c_bytes)

        if fi % 50 == 0 or fi == n_frames - 1:
            print(f"    [{fi+1}/{n_frames}] t={format_time(r['time'])}, "
                  f"PNG {len(png_bytes)/1024:.0f} KB")

    print(f"  Total data: {total_bytes / 1024 / 1024:.1f} MB")

    # ── Export hillshade PNG ──────────────────────────────────
    print("  Exporting hillshade...")
    hs = _compute_hillshade(dem, cs)
    hs_rgba = np.stack([hs, hs, hs, np.full_like(hs, 255)], axis=-1)
    hs_bytes = _write_png(hs_rgba)
    with open(os.path.join(data_dir, "hillshade.png"), "wb") as f:
        f.write(hs_bytes)
    print(f"  Hillshade: {len(hs_bytes)/1024:.0f} KB")

    # ── Export heightmap PNG (16-bit encoded) ─────────────────
    print("  Exporting heightmap...")
    nan_mask = np.isnan(dem)
    valid_dem = dem.copy()
    valid_dem[nan_mask] = 0.0  # fill NaN for encoding

    z_min = float(np.nanmin(dem)) if not np.all(nan_mask) else 0.0
    z_max = float(np.nanmax(dem)) if not np.all(nan_mask) else 1.0
    if z_max - z_min < 0.01:
        z_max = z_min + 1.0  # avoid division by zero

    z_norm = np.clip((valid_dem - z_min) / (z_max - z_min), 0.0, 1.0)
    z_16bit = (z_norm * 65535.0).astype(np.uint16)
    r_ch = (z_16bit >> 8).astype(np.uint8)      # high byte
    g_ch = (z_16bit & 0xFF).astype(np.uint8)     # low byte
    b_ch = np.where(nan_mask, np.uint8(0), np.uint8(255))  # validity mask
    a_ch = np.full_like(r_ch, 255)               # alpha = 255 always

    hm_rgba = np.stack([r_ch, g_ch, b_ch, a_ch], axis=-1)
    hm_bytes = _write_png(hm_rgba)
    with open(os.path.join(data_dir, "heightmap.png"), "wb") as f:
        f.write(hm_bytes)
    print(f"  Heightmap: {len(hm_bytes)/1024:.0f} KB "
          f"(z_min={z_min:.2f}, z_max={z_max:.2f})")

    # ── Download satellite image ────────────────────────────
    bbox_ll = None
    if utm_to_ll is not None:
        xll = meta["xllcorner"]
        yll = meta["yllcorner"]
        sw_lon, sw_lat = utm_to_ll(xll, yll)
        ne_lon, ne_lat = utm_to_ll(xll + ncols * cs, yll + nrows * cs)
        bbox_ll = {
            "sw_lat": float(sw_lat), "sw_lon": float(sw_lon),
            "ne_lat": float(ne_lat), "ne_lon": float(ne_lon),
        }
        print(f"  Bounding box: ({sw_lat:.6f},{sw_lon:.6f}) - "
              f"({ne_lat:.6f},{ne_lon:.6f})")

        # Download satellite tiles, reproject, and clip to basin
        sat_path = os.path.join(data_dir, "satellite.jpg")
        basin_mask = ~np.isnan(dem)   # True where DEM is valid
        sat_w = int(ncols * sat_resolution)
        sat_h = int(nrows * sat_resolution)
        # Upscale basin mask to match satellite resolution
        if sat_resolution != 1:
            from scipy.ndimage import zoom as ndzoom
            sat_mask = ndzoom(basin_mask.astype(np.float64),
                             sat_resolution, order=0) > 0.5
        else:
            sat_mask = basin_mask
        print(f"  Satellite resolution: {sat_w}x{sat_h} "
              f"({sat_resolution}x DEM)")
        _download_satellite(sw_lat, sw_lon, ne_lat, ne_lon,
                            sat_w, sat_h, sat_path,
                            basin_mask=sat_mask,
                            sat_zoom_boost=sat_zoom_boost)

    # ── Export metadata JSON ──────────────────────────────────
    metadata = {
        "width": int(ncols),
        "height": int(nrows),
        "n_timesteps": n_frames,
        "times": times,
        "vx_min": float(gvx_min),
        "vx_max": float(gvx_max),
        "vy_min": float(gvy_min),
        "vy_max": float(gvy_max),
        "cellsize": float(cs),
        "default_particles": n_particles,
        "z_min": z_min,
        "z_max": z_max,
        "h_max": float(gh_max),
        "height_exaggeration": 0.1,
    }
    if has_concentration:
        metadata["has_concentration"] = True
        metadata["conc_max"] = float(conc_max)
    if bbox_ll:
        metadata["bbox"] = bbox_ll
        metadata["has_satellite"] = True
        metadata["sat_resolution"] = sat_resolution
    meta_path = os.path.join(data_dir, "metadata.json")
    with open(meta_path, "w") as f:
        _json.dump(metadata, f, indent=2, allow_nan=False)

    # ── Generate HTML viewer ──────────────────────────────────
    print("  Generating WebGL viewer...")
    html = _generate_webgl_html()
    html_path = os.path.join(output_dir, "index.html")
    with open(html_path, "w") as f:
        f.write(html)

    print(f"\n  WebGL viewer exported to: {output_dir}/")
    print(f"  {n_frames} velocity frames + hillshade + metadata + viewer")
    print(f"\n  To view, start a local server:")
    print(f"    python3 -m http.server 8080 -d {output_dir}")
    print(f"  Then open:  http://localhost:8080")


def _find_port_holders(port: int):
    """Return a list of (pid, cmdline) tuples for every TCP process
    currently listening on ``port``. Multiple entries are common (IPv4 +
    IPv6 sockets, or stale previous runs). Uses ``lsof`` (macOS / Linux).
    Returns ``[]`` if nothing is listening or ``lsof`` is unavailable.
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
            stderr=subprocess.DEVNULL, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pids = []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        try:
            cmdline = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
        except Exception:
            cmdline = ""
        pids.append((pid, cmdline))
    return pids


def _serve_webgl_bundle(output_dir: str, port: int) -> None:
    """Start an HTTP server on ``port`` rooted at ``output_dir`` and pop
    the browser. If the port is already in use and the holder looks like a
    previous FLUXOS viewer / http.server (same script name, or a plain
    ``python -m http.server``), terminate it and retry. Foreign processes
    are left alone and we exit with a clear instruction.

    Runs until Ctrl-C.
    """
    import http.server
    import socketserver
    import functools
    import webbrowser
    import signal
    import os as _os
    import time

    url = f"http://localhost:{port}/"
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=output_dir
    )
    socketserver.TCPServer.allow_reuse_address = True

    # Safe-to-terminate fingerprints: match a previous run of this script,
    # or a generic python http.server. Never kill anything else.
    _OURS_FINGERPRINTS = ("fluxos_viewer.py", "http.server")

    def _try_bind_and_serve() -> bool:
        """Attempt to bind + serve. Returns True on normal exit (Ctrl-C),
        False if the port was not bindable.
        """
        try:
            with socketserver.TCPServer(("", port), handler) as httpd:
                print(f"\n  Serving {output_dir} on {url}")
                print("  Opening browser…   (press Ctrl-C to stop the server)")
                webbrowser.open_new_tab(url)
                httpd.serve_forever()
            return True
        except KeyboardInterrupt:
            print("\n  Server stopped.")
            return True
        except OSError as e:
            if getattr(e, "errno", None) not in (48, 98):  # EADDRINUSE
                # Something else went wrong; do not retry
                print(f"\n  Could not bind to port {port}: {e}")
                return True
            return False

    # First attempt
    if _try_bind_and_serve():
        return

    # Port is in use — find every holder, decide which are safe to reclaim.
    holders = _find_port_holders(port)
    if not holders:
        print(f"\n  Port {port} is in use but no holders could be "
              f"identified (is `lsof` installed?).")
        print(f"  Open a terminal and run:  lsof -iTCP:{port} -sTCP:LISTEN")
        print(f"  Then either kill the process manually or re-run this "
              f"command with --webgl-port <other-port>.")
        return

    ours, foreign = [], []
    for pid, cmdline in holders:
        (ours if any(fp in cmdline for fp in _OURS_FINGERPRINTS)
         else foreign).append((pid, cmdline))

    if foreign:
        print(f"\n  Port {port} is held by an unrelated process — will NOT "
              f"kill it:")
        for pid, cmdline in foreign:
            print(f"    PID {pid}:  {cmdline[:120]}")
        print(f"  Re-run this command with --webgl-port <other-port>, or "
              f"terminate that process yourself and try again.")
        return

    print(f"\n  Port {port} is held by {len(ours)} stale viewer / "
          f"http.server process{'es' if len(ours) != 1 else ''}:")
    for pid, cmdline in ours:
        print(f"    PID {pid}:  {cmdline[:120]}")
    print(f"  Terminating them and retrying…")

    # SIGTERM all of them first
    for pid, _ in ours:
        try:
            _os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Wait up to ~2 s for the port to clear
    for _ in range(20):
        time.sleep(0.1)
        if not _find_port_holders(port):
            break
    else:
        # Still held — SIGKILL any survivors (both the original holders and
        # any NEW holder that grabbed the socket in the meantime, as long
        # as it matches our fingerprint).
        for pid, cmdline in _find_port_holders(port):
            if any(fp in cmdline for fp in _OURS_FINGERPRINTS):
                try:
                    _os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        time.sleep(0.3)

    # Retry once
    if not _try_bind_and_serve():
        still = _find_port_holders(port)
        print(f"\n  Port {port} is still in use after the kill"
              + (f" (PIDs: {[p for p,_ in still]})" if still else "")
              + ".")
        print(f"  Try again with --webgl-port <other-port>.")


def _generate_webgl_html():
    """Generate the complete self-contained 3D WebGL particle viewer HTML.

    Reads the HTML from the canonical index.html template at
    supporting_scripts/2_Read_Outputs/fluxos_web/index.html. Falls back to
    the embedded copy below if the file doesn't exist (e.g. if this script
    was copied out of the repo and invoked stand-alone).
    """
    import pathlib as _pathlib
    # Canonical location: sibling of output_supporting_lib/
    _html_path = _pathlib.Path(__file__).parent.parent / 'fluxos_web' / 'index.html'
    if _html_path.exists():
        return _html_path.read_text()
    # Fallback: embedded (may be out-of-date)
    return '''\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FLUXOS Flood Simulation — 3D Flow Viewer</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 100%; height: 100%; overflow: hidden; background: #0a0a12; }
canvas#gl { width: 100%; height: 100%; display: block; }
#controls {
    position: fixed; bottom: 16px; left: 16px;
    background: rgba(5,8,20,0.82); color: #b0c4de;
    padding: 16px 20px; border-radius: 10px;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px; min-width: 300px;
    border: 1px solid rgba(60,90,140,0.3);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    user-select: none;
}
#controls h2 { font-size: 15px; color: #e0ecff; margin-bottom: 12px;
    font-weight: 600; letter-spacing: 0.5px; }
.ctrl-row { margin-bottom: 8px; display: flex; align-items: center;
    justify-content: space-between; }
.ctrl-row label { flex: 0 0 100px; color: #8899bb; }
.ctrl-row input[type=range] { flex: 1; margin: 0 8px; }
.ctrl-row .val { flex: 0 0 60px; text-align: right; color: #ccddef;
    font-variant-numeric: tabular-nums; }
.btn { background: rgba(40,60,100,0.5); color: #b0c4de; border: 1px solid
    rgba(80,120,180,0.4); border-radius: 5px; padding: 6px 14px;
    cursor: pointer; font-family: inherit; font-size: 12px;
    transition: background 0.15s; }
.btn:hover { background: rgba(60,90,140,0.6); }
.btn.active { background: rgba(80,140,220,0.4); border-color: rgba(100,160,240,0.6); }
#info { position: fixed; top: 16px; right: 16px;
    background: rgba(5,8,20,0.7); color: #8899bb; padding: 10px 14px;
    border-radius: 8px; font-family: 'SF Mono', monospace; font-size: 11px;
    border: 1px solid rgba(60,90,140,0.2); pointer-events: none; }
#info div { margin-bottom: 3px; }
#info .hl { color: #ccddef; }
#colorbar-wrap { position: fixed; bottom: 20px; right: 16px;
    background: rgba(5,8,20,0.7); padding: 8px 12px 6px;
    border-radius: 8px; border: 1px solid rgba(60,90,140,0.2); }
#conc-colorbar-wrap { position: fixed; bottom: 80px; right: 16px;
    background: rgba(5,8,20,0.7); padding: 8px 12px 6px;
    border-radius: 8px; border: 1px solid rgba(60,90,140,0.2); }
#conc-colorbar-wrap .cb-title { font-family: monospace; font-size: 11px;
    color: #ffffff; margin-bottom: 4px; }
#conc-colorbar-wrap .cb-labels { display: flex; justify-content: space-between;
    font-family: monospace; font-size: 10px; color: #ffffff; margin-top: 2px; }
#colorbar-wrap .cb-title { font-family: monospace; font-size: 11px;
    color: #8899bb; margin-bottom: 4px; }
#colorbar-wrap .cb-labels { display: flex; justify-content: space-between;
    font-family: monospace; font-size: 10px; color: #8899bb; margin-top: 2px; }
canvas#colorbar { display: block; border-radius: 3px; }
#loading-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: #0a0a12; display: flex; flex-direction: column;
    align-items: center; justify-content: center; z-index: 1000;
    transition: opacity 0.6s ease-out;
}
#loading-overlay.fade-out { opacity: 0; pointer-events: none; }
#loading-overlay .load-title {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 20px; color: #e0ecff; font-weight: 600;
    letter-spacing: 1px; margin-bottom: 32px;
}
#loading-overlay .load-bar-track {
    width: 320px; height: 4px; background: rgba(40,60,100,0.4);
    border-radius: 2px; overflow: hidden; position: relative;
}
#loading-overlay .load-bar-fill {
    height: 100%; width: 0%; border-radius: 2px;
    background: linear-gradient(90deg, #3366aa, #55aadd);
    transition: width 0.35s ease-out;
    box-shadow: 0 0 12px rgba(80,160,240,0.4);
}
#loading-overlay .load-status {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px; color: #667799; margin-top: 16px;
    letter-spacing: 0.3px; min-height: 18px;
}
#loading-overlay .load-pct {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 12px; color: #8899bb; margin-top: 8px;
}
#loading-overlay.load-error {
    background: rgba(10,10,18,0.98);
}
#loading-overlay.load-error .load-content {
    color: #ff6b6b; font-size: 14px; max-width: 600px;
    background: rgba(10,10,18,0.95); padding: 24px 28px; border-radius: 10px;
    border: 1px solid rgba(255,80,80,0.4); line-height: 1.6;
    white-space: pre-wrap; word-break: break-all; text-align: left;
    font-family: monospace;
}
#loading-overlay .error-title { font-size: 18px; color: #ff4444;
    margin-bottom: 12px; font-weight: bold; }
#loading-overlay .error-hint { color: #8899bb; font-size: 12px;
    margin-top: 16px; word-break: normal; }
input[type=range] { -webkit-appearance: none; height: 4px;
    background: rgba(60,90,140,0.4); border-radius: 2px; outline: none; }
input[type=range]::-webkit-slider-thumb { -webkit-appearance: none;
    width: 14px; height: 14px; border-radius: 50%;
    background: #5588cc; cursor: pointer; border: 2px solid #334466; }
</style>
</head>
<body>
<canvas id="gl"></canvas>

<div id="loading-overlay">
    <div class="load-title">FLUXOS Flood Simulation</div>
    <div class="load-bar-track">
        <div class="load-bar-fill" id="load-bar"></div>
    </div>
    <div class="load-status" id="load-status">Initializing...</div>
    <div class="load-pct" id="load-pct">0%</div>
</div>

<div id="controls" style="display:none;">
    <h2>FLUXOS Flood Simulation</h2>
    <div class="ctrl-row">
        <label>Time</label>
        <input type="range" id="sl-time" min="0" max="100" value="0">
        <span class="val" id="v-time">0s</span>
    </div>
    <div class="ctrl-row" style="justify-content:center; gap:8px; margin:10px 0;">
        <button class="btn active" id="btn-play">&#9654; Play</button>
        <button class="btn" id="btn-reset">&#8634; Reset</button>
        <button class="btn" id="btn-fs">&#x26F6; Fullscreen</button>
    </div>
    <div class="ctrl-row">
        <label>Speed</label>
        <input type="range" id="sl-speed" min="5" max="2000" value="1000">
        <span class="val" id="v-speed">10.0x</span>
    </div>
    <div class="ctrl-row">
        <label>Particles</label>
        <input type="range" id="sl-particles" min="10" max="18" value="10">
        <span class="val" id="v-particles">1K</span>
    </div>
    <div class="ctrl-row">
        <label>Trail length</label>
        <input type="range" id="sl-trail" min="880" max="998" value="880">
        <span class="val" id="v-trail">0.88</span>
    </div>
    <div class="ctrl-row">
        <label>Exaggeration</label>
        <input type="range" id="sl-exag" min="0" max="200" value="100">
        <span class="val" id="v-exag">0.100x</span>
    </div>
    <div class="ctrl-row" style="justify-content:center; gap:8px; margin-top:6px;">
        <button class="btn active" id="btn-water">&#128167; Water</button>
        <button class="btn" id="btn-particles">&#10024; Particles</button>
        <button class="btn" id="btn-sat" style="display:none;">&#127760; Satellite</button>
        <button class="btn" id="btn-conc" style="display:none;">&#9762; Concentration</button>
    </div>
</div>

<div id="info" style="display:none;">
    <div>t = <span class="hl" id="i-time">0s</span></div>
    <div><span class="hl" id="i-fps">--</span> fps</div>
    <div><span class="hl" id="i-particles">--</span> particles</div>
    <div style="margin-top:6px; font-size:10px; color:#667799;">
        Drag: rotate | Scroll: zoom<br>
        Shift+drag: pan
    </div>
</div>

<div id="colorbar-wrap" style="display:none;">
    <div class="cb-title">Flow speed (m/s)</div>
    <canvas id="colorbar" width="180" height="14"></canvas>
    <div class="cb-labels"><span id="cb-min">0</span><span id="cb-max">--</span></div>
</div>

<div id="conc-colorbar-wrap" style="display:none;">
    <div class="cb-title">Concentration (mg/L)</div>
    <canvas id="conc-colorbar" width="180" height="14"></canvas>
    <div class="cb-labels"><span id="conc-cb-min">0</span><span id="conc-cb-max">--</span></div>
</div>

<!-- ═══════ GLSL SHADERS ═══════ -->

<!-- Fullscreen quad vertex shader (used for UV-space trail fade + particle update) -->
<script id="quad-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec2 a_pos;
varying vec2 v_tex;
void main() {
    v_tex = a_pos;
    gl_Position = vec4(2.0 * a_pos.x - 1.0, 1.0 - 2.0 * a_pos.y, 0.0, 1.0);
}
</script>

<!-- Particle update shader (2D, unchanged from working version) -->
<script id="update-fs" type="x-shader/x-fragment">
precision highp float;
uniform sampler2D u_particles;
uniform sampler2D u_velocity;
uniform sampler2D u_velocity_next;
uniform sampler2D u_spawn_tex;
uniform float u_spawn_count;
uniform float u_time_frac;
uniform float u_rand_seed;
uniform float u_speed_factor;
uniform float u_drop_rate;
uniform float u_drop_rate_bump;
uniform vec2 u_vel_range_x;
uniform vec2 u_vel_range_y;
uniform vec2 u_grid_res;
varying vec2 v_tex;

float rand(vec2 co) {
    return fract(sin(dot(co, vec2(12.9898, 78.233))) * 43758.5453);
}

vec2 lookup_vel(sampler2D tex, vec2 uv) {
    vec4 t = texture2D(tex, uv);
    // R channel = ux (row/northing direction) → drives v-axis movement
    // G channel = uy (col/easting direction) → drives u-axis movement
    float vx = mix(u_vel_range_y.x, u_vel_range_y.y, t.g);  // easting vel from G
    float vy = mix(u_vel_range_x.x, u_vel_range_x.y, t.r);  // northing vel from R
    float wet = step(0.5, t.a);
    return vec2(vx, vy) * wet;
}

vec2 getSpawnPos(float idx) {
    float u = (idx + 0.5) / u_spawn_count;
    vec4 s = texture2D(u_spawn_tex, vec2(u, 0.5));
    return vec2(s.r + s.b / 255.0, s.g + s.a / 255.0);
}

void main() {
    vec4 enc = texture2D(u_particles, v_tex);
    vec2 pos = vec2(enc.r / 255.0 + enc.b, enc.g / 255.0 + enc.a);

    vec4 curVel = texture2D(u_velocity, pos);
    bool curWet = curVel.a > 0.5;

    vec2 v0 = lookup_vel(u_velocity, pos);
    vec2 v1 = lookup_vel(u_velocity_next, pos);
    vec2 vel = mix(v0, v1, u_time_frac);
    float spd = length(vel);

    vec2 off = vec2(
        vel.x * u_speed_factor / u_grid_res.x,
       -vel.y * u_speed_factor / u_grid_res.y
    );
    vec2 np2 = pos + off;

    bool oob = np2.x < 0.0 || np2.x > 1.0 || np2.y < 0.0 || np2.y > 1.0;
    bool destDry = texture2D(u_velocity, np2).a < 0.5;

    float s1 = rand(pos + u_rand_seed);
    float drop = u_drop_rate + spd * u_drop_rate_bump;

    bool needRespawn = oob || !curWet || destDry || s1 > (1.0 - drop);

    if (needRespawn) {
        float ridx = floor(rand(v_tex + u_rand_seed + 1.3) * u_spawn_count);
        np2 = getSpawnPos(ridx);
    }

    vec2 f = fract(np2 * 255.0);
    vec2 i = floor(np2 * 255.0) / 255.0;
    gl_FragColor = vec4(f.x, f.y, i.x, i.y);
}
</script>

<!-- Draw particles in UV space (for trail accumulation) -->
<script id="draw-uv-vs" type="x-shader/x-vertex">
precision mediump float;
attribute float a_index;
uniform sampler2D u_particles;
uniform float u_particles_res;
uniform sampler2D u_velocity;
uniform sampler2D u_velocity_next;
uniform float u_time_frac;
uniform vec2 u_vel_range_x;
uniform vec2 u_vel_range_y;
uniform float u_point_size;
varying float v_speed_t;

void main() {
    float col = mod(a_index, u_particles_res);
    float row = floor(a_index / u_particles_res);
    vec2 uv = (vec2(col, row) + 0.5) / u_particles_res;
    vec4 enc = texture2D(u_particles, uv);
    vec2 pos = vec2(enc.r / 255.0 + enc.b, enc.g / 255.0 + enc.a);

    vec4 t0 = texture2D(u_velocity, pos);
    vec4 t1 = texture2D(u_velocity_next, pos);
    vec4 vt = mix(t0, t1, u_time_frac);

    // R=ux (northing vel), G=uy (easting vel) — swap to get correct axes
    float vx = mix(u_vel_range_y.x, u_vel_range_y.y, vt.g);  // easting from G
    float vy = mix(u_vel_range_x.x, u_vel_range_x.y, vt.r);  // northing from R
    float spd = length(vec2(vx, vy));
    float mx = max(abs(u_vel_range_x.y), abs(u_vel_range_y.y));
    v_speed_t = clamp(spd / mx, 0.0, 1.0);

    float wet = step(0.5, vt.a);
    gl_PointSize = u_point_size * wet;
    // Position in UV-space clip coords (same mapping as quad-vs)
    gl_Position = vec4(2.0 * pos.x - 1.0, 1.0 - 2.0 * pos.y, 0.0, 1.0);
}
</script>

<!-- Draw particles in 3D (on terrain surface) -->
<script id="draw-3d-vs" type="x-shader/x-vertex">
precision mediump float;
attribute float a_index;
uniform sampler2D u_particles;
uniform float u_particles_res;
uniform sampler2D u_velocity;
uniform sampler2D u_velocity_next;
uniform sampler2D u_heightmap;
uniform float u_time_frac;
uniform vec2 u_vel_range_x;
uniform vec2 u_vel_range_y;
uniform float u_point_size;
uniform float u_z_min;
uniform float u_z_range;
uniform float u_exaggeration;
uniform float u_aspect;      // width/height of terrain in world units
uniform float u_h_max;       // max water depth (for decoding velocity B channel)
uniform mat4 u_mvp;
varying float v_speed_t;

void main() {
    float col = mod(a_index, u_particles_res);
    float row = floor(a_index / u_particles_res);
    vec2 uv = (vec2(col, row) + 0.5) / u_particles_res;
    vec4 enc = texture2D(u_particles, uv);
    vec2 pos = vec2(enc.r / 255.0 + enc.b, enc.g / 255.0 + enc.a);

    vec4 t0 = texture2D(u_velocity, pos);
    vec4 t1 = texture2D(u_velocity_next, pos);
    vec4 vt = mix(t0, t1, u_time_frac);

    // R=ux (northing vel), G=uy (easting vel) — swap to get correct axes
    float vx = mix(u_vel_range_y.x, u_vel_range_y.y, vt.g);  // easting from G
    float vy = mix(u_vel_range_x.x, u_vel_range_x.y, vt.r);  // northing from R
    float spd = length(vec2(vx, vy));
    float mx = max(abs(u_vel_range_x.y), abs(u_vel_range_y.y));
    v_speed_t = clamp(spd / mx, 0.0, 1.0);

    float wet = step(0.5, vt.a);

    // Sample heightmap for Z
    vec4 h = texture2D(u_heightmap, pos);
    float z_norm = h.r + h.g / 256.0;  // 16-bit decode: R=high byte, G=low byte
    float valid = step(0.5, h.b);       // B=validity (255=valid, 0=nodata)

    // Sample water depth from velocity B channel and float on water surface
    float d0 = t0.b * u_h_max;
    float d1 = t1.b * u_h_max;
    float w0 = step(0.5, t0.a);
    float w1 = step(0.5, t1.a);
    float depth = mix(d0 * w0, d1 * w1, u_time_frac);
    float water_offset = depth / u_z_range * u_exaggeration;

    // World position: x along width, z along depth (negated for right-hand coords)
    // Particles ride on water surface (terrain + water depth + small bump)
    vec3 worldPos = vec3(
        (pos.x - 0.5) * u_aspect,
        z_norm * u_exaggeration * valid + water_offset + 0.003,
        -(pos.y - 0.5)
    );

    gl_PointSize = u_point_size * wet * 2.0;
    gl_Position = u_mvp * vec4(worldPos, 1.0);
}
</script>

<script id="draw-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_color_ramp;
varying float v_speed_t;
void main() {
    gl_FragColor = texture2D(u_color_ramp, vec2(v_speed_t, 0.5));
}
</script>

<!-- Trail fade shader (UV-space FBO) -->
<script id="screen-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_screen;
uniform float u_opacity;
varying vec2 v_tex;
void main() {
    vec4 c = texture2D(u_screen, vec2(v_tex.x, 1.0 - v_tex.y));
    gl_FragColor = vec4(floor(255.0 * c * u_opacity) / 255.0);
}
</script>

<!-- Terrain vertex shader -->
<script id="terrain-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec3 a_position;   // (x_world, y_height, z_world)
attribute vec2 a_texcoord;   // (u, v) for texture lookup
uniform mat4 u_mvp;
varying vec2 v_uv;
varying float v_height;
varying vec3 v_world_pos;

void main() {
    v_uv = a_texcoord;
    v_height = a_position.y;
    v_world_pos = a_position;
    gl_Position = u_mvp * vec4(a_position, 1.0);
}
</script>

<!-- Terrain fragment shader — dynamic lighting, AO, fog -->
<script id="terrain-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_hillshade;
uniform sampler2D u_satellite;
uniform sampler2D u_trails;
uniform sampler2D u_heightmap;
uniform float u_dark;
uniform float u_use_satellite;
uniform vec3 u_sun_dir;
uniform vec3 u_eye;
uniform vec2 u_hm_texel;        // 1/width, 1/height for heightmap gradient
uniform float u_exaggeration;
varying vec2 v_uv;
varying float v_height;
varying vec3 v_world_pos;

void main() {
    float valid = texture2D(u_heightmap, v_uv).b;
    if (valid < 0.5) discard;

    // ── Compute terrain normal from heightmap gradient ──
    float hL = texture2D(u_heightmap, v_uv - vec2(u_hm_texel.x, 0.0)).r;
    float hR = texture2D(u_heightmap, v_uv + vec2(u_hm_texel.x, 0.0)).r;
    float hD = texture2D(u_heightmap, v_uv + vec2(0.0, u_hm_texel.y)).r;
    float hU = texture2D(u_heightmap, v_uv - vec2(0.0, u_hm_texel.y)).r;
    float hC = texture2D(u_heightmap, v_uv).r;
    vec3 terrainNormal = normalize(vec3(
        (hL - hR) * u_exaggeration * 4.0,
        1.0,
        (hU - hD) * u_exaggeration * 4.0
    ));

    // ── Lightweight AO: concavity from neighbor heights ──
    float ao = clamp(1.0 - (4.0 * hC - hL - hR - hD - hU) * u_exaggeration * 15.0, 0.15, 1.0);

    // ── Base albedo ──
    vec3 base;
    if (u_use_satellite > 0.5) {
        base = texture2D(u_satellite, v_uv).rgb;
    } else {
        base = texture2D(u_hillshade, v_uv).rgb;
    }

    // ── Dynamic directional lighting ──
    float NdotL = max(dot(terrainNormal, u_sun_dir), 0.0);
    vec3 ambient = vec3(0.18, 0.18, 0.22);   // neutral shadow fill
    vec3 sunCol  = vec3(1.0, 0.95, 0.85);    // warm sunlight
    vec3 lit = base * (ambient + sunCol * NdotL * 0.85) * ao;

    // ── Trail overlay ──
    vec3 tr = texture2D(u_trails, vec2(v_uv.x, 1.0 - v_uv.y)).rgb;
    lit = min(vec3(1.0), lit + tr);

    // ── Atmospheric fog ──
    float dist = length(v_world_pos - u_eye);
    float fogAmount = 1.0 - exp(-dist * dist * 0.8);
    vec3 fogColor = vec3(0.62, 0.63, 0.68);   // warm neutral haze
    lit = mix(lit, fogColor, fogAmount * 0.35);

    gl_FragColor = vec4(lit, 1.0);
}
</script>

<!-- Water surface vertex shader -->
<script id="water-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec3 a_position;   // terrain mesh positions (x, y_terrain, z)
attribute vec2 a_texcoord;   // (u, v) for texture lookup
uniform mat4 u_mvp;
uniform sampler2D u_velocity;       // current velocity frame (B=depth)
uniform sampler2D u_velocity_next;  // next velocity frame
uniform sampler2D u_heightmap;      // terrain heightmap for precise elevation
uniform float u_time_frac;
uniform float u_h_max;              // global max depth (for decoding B channel)
uniform float u_z_range;
uniform float u_exaggeration;
uniform vec2 u_vel_range_x;         // vx_min, vx_max (northing from R)
uniform vec2 u_vel_range_y;         // vy_min, vy_max (easting from G)

varying vec2 v_uv;
varying float v_depth;
varying vec3 v_world_pos;   // world-space position for view direction
varying vec2 v_flow;         // flow velocity in m/s
varying float v_speed;       // flow speed magnitude

void main() {
    v_uv = a_texcoord;

    // Sample water depth from B channel of velocity texture
    vec4 v0 = texture2D(u_velocity, a_texcoord);
    vec4 v1 = texture2D(u_velocity_next, a_texcoord);
    float d0 = v0.b * u_h_max;
    float d1 = v1.b * u_h_max;
    float wet0 = step(0.5, v0.a);
    float wet1 = step(0.5, v1.a);
    float depth = mix(d0 * wet0, d1 * wet1, u_time_frac);
    v_depth = depth;

    // Decode flow velocity (same axis-swap as particle shaders)
    vec4 vt = mix(v0, v1, u_time_frac);
    float vx = mix(u_vel_range_y.x, u_vel_range_y.y, vt.g);  // easting from G
    float vy = mix(u_vel_range_x.x, u_vel_range_x.y, vt.r);  // northing from R
    float wet = mix(wet0, wet1, u_time_frac);
    v_flow = vec2(vx, vy) * wet;
    v_speed = length(v_flow);

    // Sample precise terrain height from heightmap (16-bit decode)
    vec4 hm = texture2D(u_heightmap, a_texcoord);
    float z_norm = hm.r + hm.g / 256.0;
    float valid = step(0.5, hm.b);

    // Compute water surface height = terrain + depth (both in normalized units)
    float terrain_y = z_norm * u_exaggeration * valid;
    float water_offset = depth / u_z_range * u_exaggeration;

    // Add small constant bump so thin water layers are visibly above terrain
    float bump = step(0.001, depth) * 0.002;

    vec3 pos = a_position;
    pos.y = terrain_y + water_offset + bump;
    v_world_pos = pos;

    gl_Position = u_mvp * vec4(pos, 1.0);
}
</script>

<!-- Water surface fragment shader — cinematic water with sky reflection, SSS, bloom-ready HDR -->
<script id="water-fs" type="x-shader/x-fragment">
precision mediump float;

varying vec2 v_uv;
varying float v_depth;
varying vec3 v_world_pos;
varying vec2 v_flow;
varying float v_speed;

uniform float u_h_max;
uniform float u_anim_time;
uniform vec3 u_eye;
uniform vec2 u_texel_size;
uniform sampler2D u_velocity;
uniform sampler2D u_velocity_next;
uniform float u_time_frac;
uniform vec3 u_sun_dir;

// ── Simplex 2D noise (Ashima Arts / Ian McEwan) ──
vec3 mod289_3(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec2 mod289_2(vec2 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec3 permute(vec3 x) { return mod289_3(((x * 34.0) + 1.0) * x); }

float snoise(vec2 v) {
    const vec4 C = vec4(0.211324865405187, 0.366025403784439,
                       -0.577350269189626, 0.024390243902439);
    vec2 i  = floor(v + dot(v, C.yy));
    vec2 x0 = v - i + dot(i, C.xx);
    vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    vec4 x12 = x0.xyxy + C.xxzz;
    x12.xy -= i1;
    i = mod289_2(i);
    vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0))
                             + i.x + vec3(0.0, i1.x, 1.0));
    vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy),
                            dot(x12.zw, x12.zw)), 0.0);
    m = m * m;
    m = m * m;
    vec3 x_ = 2.0 * fract(p * C.www) - 1.0;
    vec3 h = abs(x_) - 0.5;
    vec3 ox = floor(x_ + 0.5);
    vec3 a0 = x_ - ox;
    m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);
    vec3 g;
    g.x = a0.x * x0.x + h.x * x0.y;
    g.yz = a0.yz * x12.xz + h.yz * x12.yw;
    return 130.0 * dot(m, g);
}

float fbm(vec2 p) {
    float f = 0.5000 * snoise(p); p *= 2.02;
    f      += 0.2500 * snoise(p);
    return f;
}

// ── Procedural sky color (matches sky-fs) ──
vec3 getSkyColor(vec3 ray) {
    float y = ray.y;
    vec3 zenith  = vec3(0.18, 0.30, 0.65);
    vec3 horizon = vec3(0.55, 0.60, 0.72);
    float skyMix = smoothstep(-0.02, 0.45, y);
    vec3 col = mix(horizon, zenith, skyMix);
    // Horizon glow
    float hGlow = exp(-abs(y) * 8.0);
    col = mix(col, vec3(0.75, 0.55, 0.35), hGlow * 0.35);
    // Sun glow in reflection
    float sunDot = max(dot(ray, u_sun_dir), 0.0);
    col += vec3(1.0, 0.95, 0.8) * pow(sunDot, 32.0) * 0.3;
    return col;
}

void main() {
    if (v_depth < 0.001) discard;

    // ── 1. Depth normalization ──
    float t = clamp(v_depth / (u_h_max * 0.5), 0.0, 1.0);

    // ── 2. Flow-responsive animated normals ──
    float flow_speed = clamp(v_speed / 2.0, 0.0, 1.0);
    float noise_scale = 40.0;
    float time_scale = 0.8;
    vec2 flow_offset = v_flow * u_anim_time * 0.3;
    vec2 p = v_uv * noise_scale;
    float n1 = fbm(p + vec2(u_anim_time * time_scale * 0.4, 0.0) - flow_offset);
    float n2 = fbm(p + vec2(0.0, u_anim_time * time_scale * 0.3) - flow_offset * 0.7
                     + vec2(5.2, 1.3));
    float amplitude = 0.15 + 0.6 * flow_speed;
    vec3 normal = normalize(vec3(n1 * amplitude, 1.0, n2 * amplitude));

    // ── 3. View direction and Fresnel ──
    vec3 view_dir = normalize(u_eye - v_world_pos);
    float NdotV = max(dot(normal, view_dir), 0.0);
    float F0 = 0.04;
    float fresnel = F0 + (1.0 - F0) * pow(1.0 - NdotV, 5.0);
    fresnel = clamp(fresnel, 0.0, 1.0);

    // ── 4. Sky reflection (procedural, matches sky dome) ──
    vec3 reflDir = reflect(-view_dir, normal);
    vec3 skyReflection = getSkyColor(reflDir);

    // ── 5. HDR Specular highlight (sun) — allows values > 1 for bloom ──
    vec3 sun_color = vec3(1.0, 0.95, 0.8);
    vec3 half_vec = normalize(u_sun_dir + view_dir);
    float NdotH = max(dot(normal, half_vec), 0.0);
    float shininess = mix(256.0, 24.0, flow_speed);
    float spec = pow(NdotH, shininess);
    vec3 specular = sun_color * mix(1.0, 2.5, t) * spec;   // HDR: can exceed 1.0

    // ── 6. Depth-dependent base color with subsurface scattering ──
    vec3 shallow_col = vec3(0.20, 0.50, 0.80);
    vec3 deep_col    = vec3(0.02, 0.06, 0.30);
    vec3 water_col   = mix(shallow_col, deep_col, t);
    float NdotL = max(dot(normal, u_sun_dir), 0.0);
    vec3 lit_water = water_col * (0.18 + 0.45 * NdotL);

    // Subsurface scattering: warm glow through shallow water
    float sss_dot = max(dot(view_dir, -u_sun_dir), 0.0);
    float sss = pow(sss_dot, 4.0) * (1.0 - t) * 0.5;
    vec3 sss_color = vec3(0.15, 0.45, 0.35);
    lit_water += sss_color * sss;

    // ── 7. Edge foam / white-water ──
    //
    // Toned down from earlier: on large, slow inundations the shallow-
    // fringe term (depth_edge) and the small-gradient term were firing
    // over most of the domain, giving a "cloudy" look that drowned out
    // the depth shading. Thresholds raised and composite strength cut
    // so foam only shows on true fast/step features (spillways,
    // wavefronts, channel banks) while calm pools stay visibly blue.
    float depth_edge = smoothstep(0.02, 0.08, t) * (1.0 - smoothstep(0.08, 0.22, t));
    vec4 v_cur = texture2D(u_velocity, v_uv);
    vec4 v_nxt = texture2D(u_velocity_next, v_uv);
    float d_here = mix(v_cur.b, v_nxt.b, u_time_frac) * u_h_max;
    float d_right = mix(
        texture2D(u_velocity, v_uv + vec2(u_texel_size.x, 0.0)).b,
        texture2D(u_velocity_next, v_uv + vec2(u_texel_size.x, 0.0)).b,
        u_time_frac) * u_h_max;
    float d_up = mix(
        texture2D(u_velocity, v_uv + vec2(0.0, u_texel_size.y)).b,
        texture2D(u_velocity_next, v_uv + vec2(0.0, u_texel_size.y)).b,
        u_time_frac) * u_h_max;
    float depth_gradient = length(vec2(d_here - d_right, d_here - d_up));
    float gradient_foam = smoothstep(0.20, 0.60, depth_gradient);
    float velocity_foam = smoothstep(1.2, 2.5, v_speed);
    float foam_noise = snoise(v_uv * 80.0 + u_anim_time * vec2(1.2, 0.8)) * 0.5 + 0.5;
    float foam = max(max(depth_edge, gradient_foam), velocity_foam) * foam_noise;
    foam = clamp(foam, 0.0, 1.0);
    vec3 foam_color = vec3(0.90, 0.95, 1.0);

    // ── Composite ──
    // Was 0.6 — dropped to 0.30 so the water colour still reads through
    // the foam mask (foam visible as hint, not as whiteout).
    vec3 final_col = mix(lit_water, foam_color, foam * 0.30);
    final_col += specular;
    // Sky reflection blended by Fresnel
    final_col = mix(final_col, skyReflection, fresnel * 0.55);

    // ── Atmospheric fog (match terrain) ──
    float dist = length(v_world_pos - u_eye);
    float fogAmount = 1.0 - exp(-dist * dist * 0.8);
    vec3 fogColor = vec3(0.62, 0.63, 0.68);
    final_col = mix(final_col, fogColor, fogAmount * 0.35);

    // ── Alpha: Fresnel-modulated transparency ──
    float base_alpha = mix(0.4, 0.88, t);
    float final_alpha = clamp(base_alpha + fresnel * 0.4 + foam * 0.10, 0.0, 0.95);

    gl_FragColor = vec4(final_col, final_alpha);
}
</script>

<!-- Concentration overlay vertex shader (reuses terrain mesh) -->
<script id="conc-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec3 a_position;
attribute vec2 a_texcoord;
uniform mat4 u_mvp;
uniform sampler2D u_concentration;
uniform sampler2D u_heightmap;
uniform float u_h_max;
uniform float u_z_range;
uniform float u_exaggeration;
uniform sampler2D u_velocity;
uniform sampler2D u_velocity_next;
uniform float u_time_frac;
varying vec2 v_uv;
varying float v_conc_t;

void main() {
    v_uv = a_texcoord;

    // Sample concentration
    float conc = texture2D(u_concentration, a_texcoord).r;
    float conc_wet = texture2D(u_concentration, a_texcoord).a;
    v_conc_t = conc * step(0.5, conc_wet);

    // Sample water depth for surface elevation
    vec4 v0 = texture2D(u_velocity, a_texcoord);
    vec4 v1 = texture2D(u_velocity_next, a_texcoord);
    float d0 = v0.b * u_h_max;
    float d1 = v1.b * u_h_max;
    float wet0 = step(0.5, v0.a);
    float wet1 = step(0.5, v1.a);
    float depth = mix(d0 * wet0, d1 * wet1, u_time_frac);

    // Sample terrain height
    vec4 hm = texture2D(u_heightmap, a_texcoord);
    float z_norm = hm.r + hm.g / 256.0;
    float valid = step(0.5, hm.b);

    float terrain_y = z_norm * u_exaggeration * valid;
    float water_offset = depth / u_z_range * u_exaggeration;
    float bump = step(0.001, depth) * 0.004;  // slightly above water surface

    vec3 pos = a_position;
    pos.y = terrain_y + water_offset + bump;
    gl_Position = u_mvp * vec4(pos, 1.0);
}
</script>

<!-- Concentration overlay fragment shader -->
<script id="conc-fs" type="x-shader/x-fragment">
precision mediump float;
varying vec2 v_uv;
varying float v_conc_t;

// Viridis-like yellow-orange-red heat ramp
vec3 concColor(float t) {
    // Green to yellow to orange to red
    vec3 c0 = vec3(0.0, 0.5, 0.0);   // green (low)
    vec3 c1 = vec3(1.0, 1.0, 0.0);   // yellow
    vec3 c2 = vec3(1.0, 0.5, 0.0);   // orange
    vec3 c3 = vec3(1.0, 0.0, 0.0);   // red (high)
    if (t < 0.333) return mix(c0, c1, t * 3.0);
    if (t < 0.666) return mix(c1, c2, (t - 0.333) * 3.0);
    return mix(c2, c3, (t - 0.666) * 3.0);
}

void main() {
    if (v_conc_t < 0.004) discard;  // no concentration
    float t = clamp(v_conc_t, 0.0, 1.0);
    vec3 col = concColor(t);
    float alpha = mix(0.4, 0.85, t);
    gl_FragColor = vec4(col, alpha);
}
</script>

<!-- Post-processing quad vertex shader (no Y-flip, unlike quad-vs which flips for trail system) -->
<script id="post-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec2 a_pos;
varying vec2 v_tex;
void main() {
    v_tex = a_pos;
    gl_Position = vec4(2.0 * a_pos.x - 1.0, 2.0 * a_pos.y - 1.0, 0.0, 1.0);
}
</script>

<!-- Sky dome vertex shader (fullscreen quad → world-space ray) -->
<script id="sky-vs" type="x-shader/x-vertex">
precision mediump float;
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
    v_uv = a_pos;
    gl_Position = vec4(2.0 * a_pos.x - 1.0, 2.0 * a_pos.y - 1.0, 0.9999, 1.0);
}
</script>

<!-- Sky dome fragment shader — procedural atmosphere with sun -->
<script id="sky-fs" type="x-shader/x-fragment">
precision mediump float;
varying vec2 v_uv;
uniform mat4 u_inv_vp;
uniform vec3 u_sun_dir;
uniform vec3 u_eye;

void main() {
    // Reconstruct world-space ray direction from clip-space UV
    // post-vs maps a_pos directly: (0,0)→clip(-1,-1), (1,1)→clip(1,1)
    vec4 clip = vec4(2.0 * v_uv.x - 1.0, 2.0 * v_uv.y - 1.0, 1.0, 1.0);
    vec4 world4 = u_inv_vp * clip;
    vec3 ray = normalize(world4.xyz / world4.w - u_eye);

    float y = ray.y;

    // Sky gradient: deep blue zenith → warm haze at horizon
    vec3 zenith  = vec3(0.18, 0.30, 0.65);
    vec3 horizon = vec3(0.55, 0.60, 0.72);
    vec3 ground  = vec3(0.15, 0.13, 0.12);

    float skyMix = smoothstep(-0.02, 0.45, y);
    vec3 skyCol = mix(horizon, zenith, skyMix);

    // Below horizon: dark ground
    if (y < 0.0) {
        skyCol = mix(horizon, ground, smoothstep(0.0, -0.15, y));
    }

    // Atmospheric scattering near horizon (warm orange tint)
    float horizonGlow = exp(-abs(y) * 8.0);
    vec3 scatterCol = vec3(0.75, 0.55, 0.35);
    skyCol = mix(skyCol, scatterCol, horizonGlow * 0.35);

    // Sun disc + glow
    float sunDot = max(dot(ray, u_sun_dir), 0.0);
    float sunDisc = smoothstep(0.9994, 0.9998, sunDot);
    float sunGlow = pow(sunDot, 256.0) * 1.5;
    float sunHalo = pow(sunDot, 32.0) * 0.4;
    vec3 sunColor = vec3(1.0, 0.95, 0.8);

    skyCol += sunColor * (sunDisc * 3.0 + sunGlow + sunHalo);

    gl_FragColor = vec4(skyCol, 1.0);
}
</script>

<!-- Bright extraction fragment shader (for bloom) -->
<script id="extract-bright-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_scene;
varying vec2 v_tex;
void main() {
    vec3 c = texture2D(u_scene, v_tex).rgb;
    float lum = dot(c, vec3(0.2126, 0.7152, 0.0722));
    float knee = 0.7;
    float soft = clamp((lum - knee) / (1.0 - knee + 0.001), 0.0, 1.0);
    soft = soft * soft;
    float contrib = max(soft, step(1.0, lum));
    gl_FragColor = vec4(c * contrib, 1.0);
}
</script>

<!-- Gaussian blur fragment shader (separable, 9-tap) -->
<script id="blur-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_image;
uniform vec2 u_direction;  // (1/w, 0) for horizontal, (0, 1/h) for vertical
varying vec2 v_tex;
void main() {
    vec3 result = vec3(0.0);
    result += texture2D(u_image, v_tex - 4.0 * u_direction).rgb * 0.0162;
    result += texture2D(u_image, v_tex - 3.0 * u_direction).rgb * 0.0540;
    result += texture2D(u_image, v_tex - 2.0 * u_direction).rgb * 0.1218;
    result += texture2D(u_image, v_tex - 1.0 * u_direction).rgb * 0.1964;
    result += texture2D(u_image, v_tex                     ).rgb * 0.2270;
    result += texture2D(u_image, v_tex + 1.0 * u_direction).rgb * 0.1964;
    result += texture2D(u_image, v_tex + 2.0 * u_direction).rgb * 0.1218;
    result += texture2D(u_image, v_tex + 3.0 * u_direction).rgb * 0.0540;
    result += texture2D(u_image, v_tex + 4.0 * u_direction).rgb * 0.0162;
    gl_FragColor = vec4(result, 1.0);
}
</script>

<!-- Composite fragment shader: bloom + ACES tone mapping + FXAA + vignette -->
<script id="composite-fs" type="x-shader/x-fragment">
precision mediump float;
uniform sampler2D u_scene;
uniform sampler2D u_bloom;
uniform vec2 u_texel;       // 1.0/width, 1.0/height
uniform float u_bloom_strength;
varying vec2 v_tex;

vec3 ACESFilm(vec3 x) {
    float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
    return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

void main() {
    // Sample scene and bloom
    vec3 scene = texture2D(u_scene, v_tex).rgb;
    vec3 bloom = texture2D(u_bloom, v_tex).rgb;
    vec3 col = scene + bloom * u_bloom_strength;

    // ACES tone mapping (handles HDR → LDR compression)
    col = ACESFilm(col);

    // Simplified FXAA: sample neighbors, blend high-contrast edges
    vec3 rgbN = texture2D(u_scene, v_tex + vec2(0.0, -u_texel.y)).rgb;
    vec3 rgbS = texture2D(u_scene, v_tex + vec2(0.0,  u_texel.y)).rgb;
    vec3 rgbE = texture2D(u_scene, v_tex + vec2( u_texel.x, 0.0)).rgb;
    vec3 rgbW = texture2D(u_scene, v_tex + vec2(-u_texel.x, 0.0)).rgb;
    vec3 luma = vec3(0.299, 0.587, 0.114);
    float lumC = dot(col, luma);
    float lumN = dot(ACESFilm(rgbN + bloom * u_bloom_strength), luma);
    float lumS = dot(ACESFilm(rgbS + bloom * u_bloom_strength), luma);
    float lumE = dot(ACESFilm(rgbE + bloom * u_bloom_strength), luma);
    float lumW = dot(ACESFilm(rgbW + bloom * u_bloom_strength), luma);
    float lumMin = min(lumC, min(min(lumN, lumS), min(lumE, lumW)));
    float lumMax = max(lumC, max(max(lumN, lumS), max(lumE, lumW)));
    float lumRange = lumMax - lumMin;
    if (lumRange > max(0.0312, lumMax * 0.125)) {
        vec3 avg = ACESFilm((rgbN + rgbS + rgbE + rgbW) * 0.25 + bloom * u_bloom_strength);
        col = mix(col, avg, 0.5);
    }

    // Vignette
    vec2 uv = v_tex - 0.5;
    float vig = 1.0 - dot(uv, uv) * 0.8;
    vig = clamp(vig * vig, 0.0, 1.0);
    col *= mix(0.3, 1.0, vig);

    gl_FragColor = vec4(col, 1.0);
}
</script>

<!-- ═══════ JAVASCRIPT ═══════ -->
<script>
'use strict';

// ═══════════════════════════════════════════════════════════════════
//  MATRIX MATH (minimal mat4 library — no dependencies)
// ═══════════════════════════════════════════════════════════════════

const Mat4 = {
    create() { const m = new Float32Array(16); m[0]=m[5]=m[10]=m[15]=1; return m; },

    identity(m) { m.fill(0); m[0]=m[5]=m[10]=m[15]=1; return m; },

    multiply(out, a, b) {
        for (let i = 0; i < 4; i++) {
            const a0=a[i], a4=a[i+4], a8=a[i+8], a12=a[i+12];
            out[i]    = a0*b[0]  + a4*b[1]  + a8*b[2]  + a12*b[3];
            out[i+4]  = a0*b[4]  + a4*b[5]  + a8*b[6]  + a12*b[7];
            out[i+8]  = a0*b[8]  + a4*b[9]  + a8*b[10] + a12*b[11];
            out[i+12] = a0*b[12] + a4*b[13] + a8*b[14] + a12*b[15];
        }
        return out;
    },

    perspective(out, fovy, aspect, near, far) {
        const f = 1.0 / Math.tan(fovy / 2);
        const nf = 1 / (near - far);
        out.fill(0);
        out[0] = f / aspect;
        out[5] = f;
        out[10] = (far + near) * nf;
        out[11] = -1;
        out[14] = 2 * far * near * nf;
        return out;
    },

    lookAt(out, eye, center, up) {
        let x0, x1, x2, y0, y1, y2, z0, z1, z2, len;
        z0 = eye[0]-center[0]; z1 = eye[1]-center[1]; z2 = eye[2]-center[2];
        len = 1/Math.sqrt(z0*z0+z1*z1+z2*z2);
        z0*=len; z1*=len; z2*=len;
        x0 = up[1]*z2 - up[2]*z1;
        x1 = up[2]*z0 - up[0]*z2;
        x2 = up[0]*z1 - up[1]*z0;
        len = Math.sqrt(x0*x0+x1*x1+x2*x2);
        if(len<1e-6){x0=0;x1=0;x2=0;}else{len=1/len;x0*=len;x1*=len;x2*=len;}
        y0=z1*x2-z2*x1; y1=z2*x0-z0*x2; y2=z0*x1-z1*x0;
        len=Math.sqrt(y0*y0+y1*y1+y2*y2);
        if(len<1e-6){y0=0;y1=0;y2=0;}else{len=1/len;y0*=len;y1*=len;y2*=len;}
        out[0]=x0;out[1]=y0;out[2]=z0;out[3]=0;
        out[4]=x1;out[5]=y1;out[6]=z1;out[7]=0;
        out[8]=x2;out[9]=y2;out[10]=z2;out[11]=0;
        out[12]=-(x0*eye[0]+x1*eye[1]+x2*eye[2]);
        out[13]=-(y0*eye[0]+y1*eye[1]+y2*eye[2]);
        out[14]=-(z0*eye[0]+z1*eye[1]+z2*eye[2]);
        out[15]=1;
        return out;
    },

    invert(out, a) {
        const a00=a[0],a01=a[1],a02=a[2],a03=a[3],
              a10=a[4],a11=a[5],a12=a[6],a13=a[7],
              a20=a[8],a21=a[9],a22=a[10],a23=a[11],
              a30=a[12],a31=a[13],a32=a[14],a33=a[15];
        const b00=a00*a11-a01*a10, b01=a00*a12-a02*a10,
              b02=a00*a13-a03*a10, b03=a01*a12-a02*a11,
              b04=a01*a13-a03*a11, b05=a02*a13-a03*a12,
              b06=a20*a31-a21*a30, b07=a20*a32-a22*a30,
              b08=a20*a33-a23*a30, b09=a21*a32-a22*a31,
              b10=a21*a33-a23*a31, b11=a22*a33-a23*a32;
        let det=b00*b11-b01*b10+b02*b09+b03*b08-b04*b07+b05*b06;
        if(!det) return null;
        det=1.0/det;
        out[0]=(a11*b11-a12*b10+a13*b09)*det;
        out[1]=(a02*b10-a01*b11-a03*b09)*det;
        out[2]=(a31*b05-a32*b04+a33*b03)*det;
        out[3]=(a22*b04-a21*b05-a23*b03)*det;
        out[4]=(a12*b08-a10*b11-a13*b07)*det;
        out[5]=(a00*b11-a02*b08+a03*b07)*det;
        out[6]=(a32*b02-a30*b05-a33*b01)*det;
        out[7]=(a20*b05-a22*b02+a23*b01)*det;
        out[8]=(a10*b10-a11*b08+a13*b06)*det;
        out[9]=(a01*b08-a00*b10-a03*b06)*det;
        out[10]=(a30*b04-a31*b02+a33*b00)*det;
        out[11]=(a21*b02-a20*b04-a23*b00)*det;
        out[12]=(a11*b07-a10*b09-a12*b06)*det;
        out[13]=(a00*b09-a01*b07+a02*b06)*det;
        out[14]=(a31*b01-a30*b03-a32*b00)*det;
        out[15]=(a20*b03-a21*b01+a22*b00)*det;
        return out;
    }
};

// ═══════════════════════════════════════════════════════════════════
//  ORBIT CAMERA
// ═══════════════════════════════════════════════════════════════════

class OrbitCamera {
    constructor() {
        this.theta = -0.55;           // azimuth (radians)
        this.phi = 0.22;             // elevation (low panoramic angle)
        this.distance = 0.9;
        this.target = [0.1, 0.08, 0.05];  // shifted toward flood area
        this.fov = 45 * Math.PI / 180;
        this.near = 0.01;
        this.far = 100;
        this._dragging = false;
        this._panning = false;
        this._lastX = 0;
        this._lastY = 0;
    }

    getEye() {
        const ct = Math.cos(this.theta), st = Math.sin(this.theta);
        const cp = Math.cos(this.phi), sp = Math.sin(this.phi);
        return [
            this.target[0] + this.distance * cp * st,
            this.target[1] + this.distance * sp,
            this.target[2] + this.distance * cp * ct
        ];
    }

    getViewMatrix() {
        const view = Mat4.create();
        return Mat4.lookAt(view, this.getEye(), this.target, [0, 1, 0]);
    }

    getProjectionMatrix(aspect) {
        const proj = Mat4.create();
        return Mat4.perspective(proj, this.fov, aspect, this.near, this.far);
    }

    getMVP(aspect) {
        const mvp = Mat4.create();
        return Mat4.multiply(mvp, this.getProjectionMatrix(aspect), this.getViewMatrix());
    }

    attach(canvas) {
        canvas.addEventListener('mousedown', e => {
            if (e.shiftKey) {
                this._panning = true;
            } else {
                this._dragging = true;
            }
            this._lastX = e.clientX;
            this._lastY = e.clientY;
            e.preventDefault();
        });

        canvas.addEventListener('mousemove', e => {
            if (!this._dragging && !this._panning) return;
            const dx = e.clientX - this._lastX;
            const dy = e.clientY - this._lastY;
            this._lastX = e.clientX;
            this._lastY = e.clientY;

            if (this._dragging) {
                this.theta -= dx * 0.005;
                this.phi = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01,
                    this.phi + dy * 0.005));
            } else if (this._panning) {
                // Pan in view plane
                const ct = Math.cos(this.theta), st = Math.sin(this.theta);
                const scale = this.distance * 0.001;
                // Right vector (horizontal)
                this.target[0] -= dx * scale * ct;
                this.target[2] += dx * scale * st;
                // Up (just Y)
                this.target[1] += dy * scale;
            }
            e.preventDefault();
        });

        window.addEventListener('mouseup', () => {
            this._dragging = false;
            this._panning = false;
        });

        canvas.addEventListener('wheel', e => {
            this.distance *= e.deltaY > 0 ? 1.1 : 0.9;
            this.distance = Math.max(0.005, Math.min(50, this.distance));
            e.preventDefault();
        }, { passive: false });

        // Touch support
        let lastTouches = null;
        canvas.addEventListener('touchstart', e => {
            if (e.touches.length === 1) {
                this._dragging = true;
                this._lastX = e.touches[0].clientX;
                this._lastY = e.touches[0].clientY;
            }
            lastTouches = e.touches;
            e.preventDefault();
        }, { passive: false });

        canvas.addEventListener('touchmove', e => {
            if (e.touches.length === 1 && this._dragging) {
                const dx = e.touches[0].clientX - this._lastX;
                const dy = e.touches[0].clientY - this._lastY;
                this._lastX = e.touches[0].clientX;
                this._lastY = e.touches[0].clientY;
                this.theta -= dx * 0.005;
                this.phi = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01,
                    this.phi + dy * 0.005));
            } else if (e.touches.length === 2 && lastTouches && lastTouches.length === 2) {
                // Pinch zoom
                const d0 = Math.hypot(
                    lastTouches[0].clientX - lastTouches[1].clientX,
                    lastTouches[0].clientY - lastTouches[1].clientY);
                const d1 = Math.hypot(
                    e.touches[0].clientX - e.touches[1].clientX,
                    e.touches[0].clientY - e.touches[1].clientY);
                this.distance *= d0 / Math.max(d1, 1);
                this.distance = Math.max(0.005, Math.min(50, this.distance));
            }
            lastTouches = e.touches;
            e.preventDefault();
        }, { passive: false });

        canvas.addEventListener('touchend', () => {
            this._dragging = false;
            lastTouches = null;
        });
    }
}

// ═══════════════════════════════════════════════════════════════════
//  COLOR RAMP + COLORBAR
// ═══════════════════════════════════════════════════════════════════

function buildColorRamp() {
    const stops = [
        [0.00, 15, 20, 80], [0.15, 30, 70, 170], [0.40, 60, 190, 255],
        [0.70, 200, 255, 255], [0.90, 255, 255, 180], [1.00, 255, 230, 80]
    ];
    const data = new Uint8Array(256 * 4);
    for (let p = 0; p < 256; p++) {
        const t = p / 255;
        let si = 0;
        for (let i = 0; i < stops.length - 1; i++) {
            if (t >= stops[i][0] && t <= stops[i+1][0]) { si = i; break; }
            if (i === stops.length - 2) si = i;
        }
        const f = (t - stops[si][0]) / Math.max(stops[si+1][0] - stops[si][0], 1e-6);
        data[p*4+0] = Math.round(stops[si][1] + (stops[si+1][1] - stops[si][1]) * f);
        data[p*4+1] = Math.round(stops[si][2] + (stops[si+1][2] - stops[si][2]) * f);
        data[p*4+2] = Math.round(stops[si][3] + (stops[si+1][3] - stops[si][3]) * f);
        data[p*4+3] = 255;
    }
    return data;
}

function drawColorbar() {
    const c = document.getElementById('colorbar');
    const ctx = c.getContext('2d');
    const ramp = buildColorRamp();
    for (let x = 0; x < c.width; x++) {
        const i = Math.floor(x / c.width * 255) * 4;
        ctx.fillStyle = `rgb(${ramp[i]},${ramp[i+1]},${ramp[i+2]})`;
        ctx.fillRect(x, 0, 1, c.height);
    }
}

// ═══════════════════════════════════════════════════════════════════
//  WEBGL HELPERS
// ═══════════════════════════════════════════════════════════════════

function createShader(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error('Shader: ' + gl.getShaderInfoLog(s));
    return s;
}
function createProgram(gl, vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS))
        throw new Error('Program: ' + gl.getProgramInfoLog(p));
    return p;
}
function createTex(gl, filter, data, w, h) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    if (data instanceof HTMLImageElement || data instanceof ImageData) {
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, data);
    } else {
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
    }
    return t;
}
function createFBO(gl, tex) {
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return fb;
}
function bindAttr(gl, buf, attr, size) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(attr);
    gl.vertexAttribPointer(attr, size, gl.FLOAT, false, 0, 0);
}

// ═══════════════════════════════════════════════════════════════════
//  TERRAIN MESH BUILDER
// ═══════════════════════════════════════════════════════════════════

function sampleHeight(heightData, imgW, imgH, fx, fy) {
    // Bilinear interpolation of 16-bit height from heightmap pixel data
    const x = fx * (imgW - 1);
    const y = fy * (imgH - 1);
    const x0 = Math.max(0, Math.min(imgW - 2, Math.floor(x)));
    const y0 = Math.max(0, Math.min(imgH - 2, Math.floor(y)));
    const x1 = x0 + 1, y1 = y0 + 1;
    const sx = x - x0, sy = y - y0;

    function decode(px, py) {
        const idx = (py * imgW + px) * 4;
        const r = heightData[idx], g = heightData[idx + 1], b = heightData[idx + 2];
        const zNorm = (r + g / 256.0) / 255.0;
        return { z: zNorm, valid: b > 128 };
    }

    const p00 = decode(x0, y0), p10 = decode(x1, y0);
    const p01 = decode(x0, y1), p11 = decode(x1, y1);

    // If any corner is invalid, use nearest valid
    const anyValid = p00.valid || p10.valid || p01.valid || p11.valid;
    if (!anyValid) return { z: 0, valid: false };

    const w00 = p00.valid ? (1-sx)*(1-sy) : 0;
    const w10 = p10.valid ? sx*(1-sy) : 0;
    const w01 = p01.valid ? (1-sx)*sy : 0;
    const w11 = p11.valid ? sx*sy : 0;
    const wSum = w00 + w10 + w01 + w11;

    return {
        z: (p00.z*w00 + p10.z*w10 + p01.z*w01 + p11.z*w11) / wSum,
        valid: anyValid
    };
}

function buildTerrainMesh(heightData, imgW, imgH, meta, exaggeration) {
    // Build mesh at half resolution — good balance of smoothness vs performance
    const stepX = Math.max(1, Math.floor(imgW / 430));
    const stepY = Math.max(1, Math.floor(imgH / 310));
    const meshW = Math.floor((imgW - 1) / stepX) + 1;
    const meshH = Math.floor((imgH - 1) / stepY) + 1;

    const zMin = meta.z_min;
    const zMax = meta.z_max;
    const zRange = zMax - zMin;
    const aspect = imgW / imgH;

    // 5 floats per vertex: x, y, z (world), u, v (texcoord)
    const verts = new Float32Array(meshW * meshH * 5);
    // Per-vertex validity mask (for skipping NoData triangles)
    const valid = new Uint8Array(meshW * meshH);
    let vi = 0;

    for (let row = 0; row < meshH; row++) {
        for (let col = 0; col < meshW; col++) {
            const idx = row * meshW + col;
            const u = Math.min(col * stepX, imgW - 1) / (imgW - 1);
            const v = Math.min(row * stepY, imgH - 1) / (imgH - 1);

            // Bilinear-interpolated height sample (removes staircase artifacts)
            const h = sampleHeight(heightData, imgW, imgH, u, v);

            const worldX = (u - 0.5) * aspect;
            const worldY = h.valid ? h.z * exaggeration : 0;
            const worldZ = -(v - 0.5);

            valid[idx] = h.valid ? 1 : 0;
            verts[vi++] = worldX;
            verts[vi++] = worldY;
            verts[vi++] = worldZ;
            verts[vi++] = u;
            verts[vi++] = v;
        }
    }

    // Build index buffer — skip triangles where any vertex is NoData
    const maxIndices = (meshW - 1) * (meshH - 1) * 6;
    const useUint32 = meshW * meshH > 65535;
    const indices = useUint32 ? new Uint32Array(maxIndices) : new Uint16Array(maxIndices);
    let ii = 0;

    for (let row = 0; row < meshH - 1; row++) {
        for (let col = 0; col < meshW - 1; col++) {
            const tl = row * meshW + col;
            const tr = tl + 1;
            const bl = tl + meshW;
            const br = bl + 1;
            // Upper triangle: tl-bl-tr
            if (valid[tl] && valid[bl] && valid[tr]) {
                indices[ii++] = tl; indices[ii++] = bl; indices[ii++] = tr;
            }
            // Lower triangle: tr-bl-br
            if (valid[tr] && valid[bl] && valid[br]) {
                indices[ii++] = tr; indices[ii++] = bl; indices[ii++] = br;
            }
        }
    }

    const nIndices = ii;
    return { verts, indices, meshW, meshH, useUint32, nIndices, aspect };
}

// ═══════════════════════════════════════════════════════════════════
//  3D FLOOD VIEWER
// ═══════════════════════════════════════════════════════════════════

class FloodViewer3D {
    constructor(canvas) {
        this.gl = canvas.getContext('webgl', {
            antialias: false, premultipliedAlpha: false, alpha: false
        });
        if (!this.gl) throw new Error('WebGL not supported');

        // Check for OES_element_index_uint extension (for large meshes)
        this._uint32Ext = this.gl.getExtension('OES_element_index_uint');

        this.canvas = canvas;
        this.camera = new OrbitCamera();
        this.camera.attach(canvas);

        this.meta = null;
        this.currentFrame = 0;
        this.timeFrac = 0;
        this.playing = true;
        this.speed = 10.0;
        this.fadeOpacity = 0.88;
        this.dropRate = 0.003;
        this.dropRateBump = 0.01;
        this.darkFactor = 0.25;
        this.pointSize = 1.5;
        this.particleRes = 32;
        this.numParticles = 32 * 32;
        this.exaggeration = 0.1;
        this.frameCache = new Map();
        this.frameCacheMax = 30;
        this.speedFactor = 0.5;
        this._lastT = 0;
        this._fpsFrames = 0;
        this._fpsTime = 0;
        this._fps = 0;

        // Terrain mesh data
        this.terrainVBO = null;
        this.terrainIBO = null;
        this.terrainMesh = null;
        this.heightmapTex = null;
        this.heightData = null;

        // Satellite imagery
        this.satelliteTex = null;
        this.useSatellite = false;
        // Water level surface
        this.showWater = true;
        // Particles off by default — the water-surface shading already
        // conveys the flood extent cleanly, and the ~65k moving points
        // can distract from depth reading and slow down weaker GPUs.
        // The toggle button (btn-particles) still lets the user enable
        // them whenever the velocity field is what they want to inspect.
        this.showParticles = false;
        // Concentration overlay
        this.showConcentration = false;
        this.concFrameCache = new Map();
    }

    _setProgress(pct, status) {
        const bar = document.getElementById('load-bar');
        const txt = document.getElementById('load-status');
        const pctEl = document.getElementById('load-pct');
        if (bar) bar.style.width = pct + '%';
        if (txt) txt.textContent = status;
        if (pctEl) pctEl.textContent = Math.round(pct) + '%';
    }

    async init() {
        // Cache-busting version string to force reload after re-export
        this._v = '?v=' + Date.now();
        this._setProgress(2, 'Fetching metadata...');
        let resp;
        try {
            resp = await fetch('data/metadata.json' + this._v);
        } catch (netErr) {
            const err = new Error('Network error fetching metadata.json — the server may be unreachable or blocking CORS.');
            err.url = 'data/metadata.json';
            throw err;
        }
        if (!resp.ok) {
            const err = new Error('Server returned HTTP ' + resp.status + ' for metadata.json');
            err.url = resp.url;
            err.status = resp.status;
            throw err;
        }
        try {
            this.meta = await resp.json();
        } catch (parseErr) {
            const err = new Error('metadata.json is not valid JSON — server may be returning an HTML error page.');
            err.url = resp.url;
            throw err;
        }
        const m = this.meta;
        this._setProgress(5, 'Metadata loaded');

        document.getElementById('sl-time').max = m.n_timesteps - 1;
        document.getElementById('cb-max').textContent =
            Math.max(Math.abs(m.vx_max), Math.abs(m.vy_max)).toFixed(2);

        // Hide time controls for single-frame (steady-state) views
        if (m.n_timesteps <= 1) {
            document.getElementById('sl-time').parentElement.style.display = 'none';
            document.getElementById('btn-play').style.display = 'none';
            document.getElementById('btn-reset').style.display = 'none';
            document.getElementById('sl-speed').parentElement.style.display = 'none';
        }

        if (m.height_exaggeration !== undefined) {
            this.exaggeration = Math.min(m.height_exaggeration, 0.2);
            document.getElementById('sl-exag').value = Math.round(this.exaggeration * 1000);
            document.getElementById('v-exag').textContent = this.exaggeration.toFixed(3) + 'x';
        }

        const simDt = m.times.length > 1 ? m.times[1] - m.times[0] : 10;
        const dtPerRenderFrame = simDt / 50.0;
        this.speedFactor = dtPerRenderFrame / m.cellsize;

        const gl = this.gl;

        // Load heightmap image and extract pixel data
        this._setProgress(10, 'Loading heightmap...');
        const hmImg = await this.loadImageElement('data/heightmap.png' + this._v);
        const hmCanvas = document.createElement('canvas');
        hmCanvas.width = hmImg.width;
        hmCanvas.height = hmImg.height;
        const hmCtx = hmCanvas.getContext('2d');
        hmCtx.drawImage(hmImg, 0, 0);
        this.heightData = hmCtx.getImageData(0, 0, hmImg.width, hmImg.height).data;

        // Create heightmap WebGL texture
        this.heightmapTex = createTex(gl, gl.LINEAR, hmImg, 0, 0);
        this._setProgress(20, 'Building terrain mesh...');

        // Build terrain mesh from heightmap
        this.terrainMesh = buildTerrainMesh(
            this.heightData, hmImg.width, hmImg.height, m, this.exaggeration);

        // Set camera aspect
        this.terrainAspect = this.terrainMesh.aspect;

        // Upload terrain mesh to GPU
        this.terrainVBO = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.terrainVBO);
        gl.bufferData(gl.ARRAY_BUFFER, this.terrainMesh.verts, gl.STATIC_DRAW);

        this.terrainIBO = gl.createBuffer();
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.terrainIBO);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, this.terrainMesh.indices, gl.STATIC_DRAW);

        console.log(`Terrain mesh: ${this.terrainMesh.meshW}x${this.terrainMesh.meshH} = `
            + `${this.terrainMesh.meshW * this.terrainMesh.meshH} vertices, `
            + `${this.terrainMesh.nIndices / 3} triangles`);

        // Compile shaders
        this._setProgress(30, 'Compiling shaders...');
        this.compileShaders(gl);
        this.createBuffers(gl);
        this.initParticleState(gl);
        this.initTrailTextures(gl);
        this.initPostFBOs();

        // Color ramp
        this.colorRampTex = createTex(gl, gl.LINEAR, buildColorRamp(), 256, 1);
        this._setProgress(35, 'Loading hillshade...');

        // Hillshade
        this.hillshadeTex = await this.loadImage(gl, 'data/hillshade.png' + this._v, gl.LINEAR);
        this._setProgress(42, 'Loading satellite imagery...');

        // Satellite imagery (optional — try to load, show toggle if available)
        if (m.has_satellite) {
            try {
                this.satelliteTex = await this.loadImage(gl, 'data/satellite.jpg' + this._v, gl.LINEAR);
                document.getElementById('btn-sat').style.display = '';
                // Enable satellite view by default when available
                this.useSatellite = true;
                document.getElementById('btn-sat').classList.add('active');
                document.getElementById('btn-sat').innerHTML = '&#127757; Hillshade';
                console.log('Satellite imagery loaded');
            } catch (e) {
                console.log('No satellite imagery available');
            }
        }
        this._setProgress(55, 'Preparing data layers...');

        // Concentration data (optional)
        if (m.has_concentration) {
            document.getElementById('btn-conc').style.display = '';
            document.getElementById('conc-cb-max').textContent =
                m.conc_max ? m.conc_max.toFixed(1) : '--';
            // Draw concentration colorbar
            const ccb = document.getElementById('conc-colorbar');
            const cctx = ccb.getContext('2d');
            for (let x = 0; x < ccb.width; x++) {
                const t = x / ccb.width;
                let r2, g2, b2;
                if (t < 0.333) {
                    const f = t * 3.0;
                    r2 = Math.round(f * 255);
                    g2 = Math.round((0.5 + 0.5 * f) * 255);
                    b2 = Math.round((1 - f) * 0);
                } else if (t < 0.666) {
                    const f = (t - 0.333) * 3.0;
                    r2 = 255;
                    g2 = Math.round((1.0 - 0.5 * f) * 255);
                    b2 = 0;
                } else {
                    const f = (t - 0.666) * 3.0;
                    r2 = 255;
                    g2 = Math.round((0.5 - 0.5 * f) * 255);
                    b2 = 0;
                }
                cctx.fillStyle = `rgb(${r2},${g2},${b2})`;
                cctx.fillRect(x, 0, 1, ccb.height);
            }
            console.log('Concentration data available, conc_max=' + m.conc_max);
            // Auto-enable concentration display for steady-state single-frame views
            if (m.n_timesteps <= 1) {
                this.showConcentration = true;
                document.getElementById('btn-conc').classList.add('active');
                document.getElementById('conc-colorbar-wrap').style.display = '';
            }
        }

        // ── Download ALL frame images into browser cache ──
        // This ensures zero network stalls during playback at any speed.
        // We fetch every PNG as an Image element (goes into browser HTTP cache),
        // then during playback loadVelFrame() loads them instantly from cache.
        this._setProgress(58, 'Downloading simulation frames...');
        const totalImages = m.n_timesteps + (m.has_concentration ? m.n_timesteps : 0);
        let downloaded = 0;

        // Helper: download a single image into browser cache
        const cacheImage = (url) => new Promise((resolve) => {
            const img = new Image();
            img.onload = () => { downloaded++; resolve(); };
            img.onerror = () => { downloaded++; resolve(); }; // don't block on errors
            img.src = url;
        });

        // Download in batches of 6 concurrent fetches to avoid overwhelming the connection
        const batchSize = 6;
        for (let batch = 0; batch < m.n_timesteps; batch += batchSize) {
            const promises = [];
            for (let j = batch; j < Math.min(batch + batchSize, m.n_timesteps); j++) {
                const pad = String(j).padStart(4, '0');
                promises.push(cacheImage(`data/velocity/v_${pad}.png${this._v}`));
                if (m.has_concentration) {
                    promises.push(cacheImage(`data/conc/c_${pad}.png${this._v}`));
                }
            }
            await Promise.all(promises);
            const pct = 58 + (downloaded / totalImages) * 34;
            this._setProgress(pct, `Downloading frames... ${downloaded}/${totalImages}`);
        }
        console.log(`Downloaded all ${totalImages} frame images into browser cache`);

        // Now build GPU textures for the starting frames
        this._setProgress(93, 'Preparing initial view...');
        let startFrame = 0;
        if (m.times) {
            for (let i = 0; i < m.times.length; i++) {
                if (m.times[i] >= 1800) { startFrame = i; break; }
            }
        } else {
            startFrame = Math.min(12, m.n_timesteps - 1);
        }
        await this.loadVelFrame(startFrame);
        this.currentFrame = startFrame;
        const nextF = Math.min(startFrame + 1, m.n_timesteps - 1);
        if (!this.frameCache.has(nextF)) await this.loadVelFrame(nextF);

        // Preload concentration frames if available
        if (m.has_concentration) {
            await this.loadConcFrame(startFrame);
            this.loadConcFrame(nextF);
        }

        console.log(`Starting at frame ${startFrame}, speedFactor: ${this.speedFactor.toFixed(4)}`);

        // Seed particles at wet positions
        const startEntry = this.frameCache.get(startFrame);
        if (startEntry && startEntry.spawnCount > 1) {
            this.initParticlesFromSpawn(gl, startEntry);
        }

        // Set camera to cinematic low panoramic view of the flood
        this.camera.target = [0.08, 0.02, -0.15];
        this.camera.distance = 0.85;
        this.camera.phi = 0.30;              // low panoramic angle (~17°)
        this.camera.theta = -0.28;           // azimuth (~-16°), shifted more right

        // Set playback speed to 10x
        this.speed = 10.0;
        document.getElementById('sl-speed').value = 1000;
        document.getElementById('v-speed').textContent = '10.0x';

        // Update time slider to match start frame
        document.getElementById('sl-time').value = startFrame;

        this._setProgress(100, 'Ready!');
        // Smooth fade-out of loading overlay
        const overlay = document.getElementById('loading-overlay');
        overlay.classList.add('fade-out');
        setTimeout(() => { overlay.style.display = 'none'; }, 700);
        document.getElementById('controls').style.display = '';
        document.getElementById('info').style.display = '';
        document.getElementById('colorbar-wrap').style.display = '';
        drawColorbar();

        this._lastT = performance.now();
        this.render();
    }

    compileShaders(gl) {
        const mkVS = id => createShader(gl, gl.VERTEX_SHADER,
            document.getElementById(id).textContent);
        const mkFS = id => createShader(gl, gl.FRAGMENT_SHADER,
            document.getElementById(id).textContent);

        const quadVS = mkVS('quad-vs');
        const drawUvVS = mkVS('draw-uv-vs');
        const draw3dVS = mkVS('draw-3d-vs');
        const terrainVS = mkVS('terrain-vs');

        this.updateProg = createProgram(gl, quadVS, mkFS('update-fs'));
        this.drawUvProg = createProgram(gl, drawUvVS, mkFS('draw-fs'));
        this.draw3dProg = createProgram(gl, draw3dVS, mkFS('draw-fs'));
        this.screenProg = createProgram(gl, quadVS, mkFS('screen-fs'));
        this.terrainProg = createProgram(gl, terrainVS, mkFS('terrain-fs'));
        this.waterProg = createProgram(gl, mkVS('water-vs'), mkFS('water-fs'));
        this.concProg = createProgram(gl, mkVS('conc-vs'), mkFS('conc-fs'));
        // Post-processing pipeline (uses post-vs: no Y-flip, unlike quad-vs)
        const postVS = mkVS('post-vs');
        this.skyProg = createProgram(gl, mkVS('sky-vs'), mkFS('sky-fs'));
        this.extractBrightProg = createProgram(gl, postVS, mkFS('extract-bright-fs'));
        this.blurProg = createProgram(gl, postVS, mkFS('blur-fs'));
        this.compositeProg = createProgram(gl, postVS, mkFS('composite-fs'));
    }

    createBuffers(gl) {
        this.quadBuf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuf);
        gl.bufferData(gl.ARRAY_BUFFER,
            new Float32Array([0,0, 1,0, 0,1, 0,1, 1,0, 1,1]), gl.STATIC_DRAW);

        this.indexBuf = gl.createBuffer();
        const idx = new Float32Array(this.numParticles);
        for (let i = 0; i < this.numParticles; i++) idx[i] = i;
        gl.bindBuffer(gl.ARRAY_BUFFER, this.indexBuf);
        gl.bufferData(gl.ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    }

    initParticleState(gl) {
        const n = this.particleRes * this.particleRes * 4;
        const data = new Uint8Array(n);
        for (let i = 0; i < this.particleRes * this.particleRes; i++) {
            const x = Math.random(), y = Math.random();
            data[i*4+0] = Math.floor(256 * (x * 255 - Math.floor(x * 255)));
            data[i*4+1] = Math.floor(256 * (y * 255 - Math.floor(y * 255)));
            data[i*4+2] = Math.floor(x * 255);
            data[i*4+3] = Math.floor(y * 255);
        }
        this.particleTex0 = createTex(gl, gl.NEAREST, data,
            this.particleRes, this.particleRes);
        this.particleTex1 = createTex(gl, gl.NEAREST,
            new Uint8Array(n), this.particleRes, this.particleRes);
        this.particleFB0 = createFBO(gl, this.particleTex0);
        this.particleFB1 = createFBO(gl, this.particleTex1);
    }

    initParticlesFromSpawn(gl, frameEntry) {
        const wp = frameEntry.wetPositions;
        const nWet = Math.floor(wp.length / 2);
        if (nWet < 1) return;

        const n = this.particleRes * this.particleRes;
        const data = new Uint8Array(n * 4);
        for (let i = 0; i < n; i++) {
            const ri = Math.floor(Math.random() * nWet);
            const x = wp[ri * 2];
            const y = wp[ri * 2 + 1];
            data[i*4+0] = Math.floor(256 * (x * 255 - Math.floor(x * 255)));
            data[i*4+1] = Math.floor(256 * (y * 255 - Math.floor(y * 255)));
            data[i*4+2] = Math.floor(x * 255);
            data[i*4+3] = Math.floor(y * 255);
        }
        gl.bindTexture(gl.TEXTURE_2D, this.particleTex0);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, this.particleRes, this.particleRes,
            0, gl.RGBA, gl.UNSIGNED_BYTE, data);
        console.log(`Initialized ${n} particles at ${nWet} wet positions`);
    }

    // Trail textures: fixed resolution matching terrain UV space
    initTrailTextures(gl) {
        const tw = this.meta.width;
        const th = this.meta.height;
        this.trailW = tw;
        this.trailH = th;
        const empty = new Uint8Array(tw * th * 4);
        if (this.trailTex0) gl.deleteTexture(this.trailTex0);
        if (this.trailTex1) gl.deleteTexture(this.trailTex1);
        if (this.trailFB0) gl.deleteFramebuffer(this.trailFB0);
        if (this.trailFB1) gl.deleteFramebuffer(this.trailFB1);
        this.trailTex0 = createTex(gl, gl.LINEAR, empty, tw, th);
        this.trailTex1 = createTex(gl, gl.LINEAR, empty, tw, th);
        this.trailFB0 = createFBO(gl, this.trailTex0);
        this.trailFB1 = createFBO(gl, this.trailTex1);
    }

    // Post-processing FBOs: scene (full-res) + bloom pipeline (1/4 res)
    initPostFBOs() {
        const gl = this.gl;
        const cw = gl.canvas.width, ch = gl.canvas.height;
        const qw = Math.max(1, Math.floor(cw / 4));
        const qh = Math.max(1, Math.floor(ch / 4));

        // Scene FBO (full-res color + depth renderbuffer)
        this.sceneColorTex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this.sceneColorTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, cw, ch, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

        this.sceneDepthRB = gl.createRenderbuffer();
        gl.bindRenderbuffer(gl.RENDERBUFFER, this.sceneDepthRB);
        gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, cw, ch);

        this.sceneFBO = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.sceneFBO);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.sceneColorTex, 0);
        gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, this.sceneDepthRB);

        // Bloom FBOs (1/4 resolution)
        this.bloomW = qw;
        this.bloomH = qh;

        function makeBloomFBO() {
            const tex = gl.createTexture();
            gl.bindTexture(gl.TEXTURE_2D, tex);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, qw, qh, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
            const fb = gl.createFramebuffer();
            gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
            gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
            return { tex, fb };
        }

        const bright = makeBloomFBO();
        this.brightTex = bright.tex;
        this.brightFBO = bright.fb;
        const blurA = makeBloomFBO();
        this.blurTexA = blurA.tex;
        this.blurFBO_A = blurA.fb;
        const blurB = makeBloomFBO();
        this.blurTexB = blurB.tex;
        this.blurFBO_B = blurB.fb;

        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    rebuildPostFBOs() {
        const gl = this.gl;
        if (!this.sceneFBO) return;
        // Delete old
        gl.deleteTexture(this.sceneColorTex);
        gl.deleteRenderbuffer(this.sceneDepthRB);
        gl.deleteFramebuffer(this.sceneFBO);
        gl.deleteTexture(this.brightTex);
        gl.deleteFramebuffer(this.brightFBO);
        gl.deleteTexture(this.blurTexA);
        gl.deleteFramebuffer(this.blurFBO_A);
        gl.deleteTexture(this.blurTexB);
        gl.deleteFramebuffer(this.blurFBO_B);
        this.initPostFBOs();
    }

    async loadImageElement(url) {
        return new Promise((res, rej) => {
            const img = new Image();
            img.onload = () => res(img);
            img.onerror = () => {
                const err = new Error('Failed to load image: ' + url);
                err.url = url;
                rej(err);
            };
            img.src = url;
        });
    }

    async loadImage(gl, url, filter) {
        return new Promise((res, rej) => {
            const img = new Image();
            img.onload = () => { res(createTex(gl, filter, img, 0, 0)); };
            img.onerror = () => {
                const err = new Error('Failed to load image: ' + url);
                err.url = url;
                rej(err);
            };
            img.src = url;
        });
    }

    async loadVelFrame(idx) {
        if (this.frameCache.has(idx)) return this.frameCache.get(idx);
        const gl = this.gl;
        const m = this.meta;

        const velUrl = `data/velocity/v_${String(idx).padStart(4,'0')}.png${this._v}`;
        const img = await new Promise((res, rej) => {
            const im = new Image();
            im.onload = () => res(im);
            im.onerror = () => {
                const err = new Error('Failed to load velocity frame: ' + velUrl);
                err.url = velUrl;
                rej(err);
            };
            im.src = velUrl;
        });

        const tex = createTex(gl, gl.LINEAR, img, 0, 0);

        const oc = document.createElement('canvas');
        oc.width = img.width; oc.height = img.height;
        const ctx = oc.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const pixels = ctx.getImageData(0, 0, img.width, img.height).data;

        const wetPositions = [];
        const step = Math.max(1, Math.floor(img.width * img.height / 50000));
        for (let i = 0; i < img.width * img.height; i += step) {
            if (pixels[i * 4 + 3] > 128) {
                const col = i % img.width;
                const row = Math.floor(i / img.width);
                wetPositions.push((col + 0.5) / img.width, (row + 0.5) / img.height);
            }
        }

        const maxSpawn = 2048;
        let nSpawn = Math.floor(wetPositions.length / 2);
        if (nSpawn > maxSpawn) nSpawn = maxSpawn;
        if (nSpawn < 1) nSpawn = 1;

        const spawnData = new Uint8Array(nSpawn * 4);
        for (let i = 0; i < nSpawn; i++) {
            let si = i;
            if (wetPositions.length / 2 > maxSpawn) {
                si = Math.floor(i * (wetPositions.length / 2) / maxSpawn);
            }
            const x = si < wetPositions.length / 2 ? wetPositions[si * 2] : 0.5;
            const y = si < wetPositions.length / 2 ? wetPositions[si * 2 + 1] : 0.5;
            spawnData[i * 4 + 0] = Math.floor(x * 255);
            spawnData[i * 4 + 1] = Math.floor(y * 255);
            spawnData[i * 4 + 2] = Math.floor((x * 255 - Math.floor(x * 255)) * 255);
            spawnData[i * 4 + 3] = Math.floor((y * 255 - Math.floor(y * 255)) * 255);
        }

        const spawnTex = createTex(gl, gl.NEAREST, spawnData, nSpawn, 1);

        this.frameCache.set(idx, { velTex: tex, spawnTex, spawnCount: nSpawn, wetPositions });
        if (this.frameCache.size > this.frameCacheMax) {
            const oldest = this.frameCache.keys().next().value;
            const entry = this.frameCache.get(oldest);
            gl.deleteTexture(entry.velTex);
            gl.deleteTexture(entry.spawnTex);
            this.frameCache.delete(oldest);
        }
        return this.frameCache.get(idx);
    }

    async loadConcFrame(idx) {
        if (this.concFrameCache.has(idx)) return this.concFrameCache.get(idx);
        const gl = this.gl;
        try {
            const tex = await this.loadImage(gl,
                `data/conc/c_${String(idx).padStart(4,'0')}.png${this._v}`, gl.LINEAR);
            this.concFrameCache.set(idx, tex);
            if (this.concFrameCache.size > this.frameCacheMax) {
                const oldest = this.concFrameCache.keys().next().value;
                gl.deleteTexture(this.concFrameCache.get(oldest));
                this.concFrameCache.delete(oldest);
            }
            return tex;
        } catch (e) {
            return null;
        }
    }

    preload() {
        for (let i = 1; i <= 15; i++) {
            const fi = (this.currentFrame + i) % this.meta.n_timesteps;
            if (!this.frameCache.has(fi)) this.loadVelFrame(fi);
            if (this.meta.has_concentration && !this.concFrameCache.has(fi)) {
                this.loadConcFrame(fi);
            }
        }
    }

    setParticleCount(power) {
        const gl = this.gl;
        this.particleRes = Math.pow(2, Math.ceil(power / 2));
        this.numParticles = this.particleRes * this.particleRes;
        if (this.particleFB0) gl.deleteFramebuffer(this.particleFB0);
        if (this.particleFB1) gl.deleteFramebuffer(this.particleFB1);
        if (this.particleTex0) gl.deleteTexture(this.particleTex0);
        if (this.particleTex1) gl.deleteTexture(this.particleTex1);
        this.initParticleState(gl);
        const entry = this.frameCache.get(this.currentFrame);
        if (entry && entry.spawnCount > 1) this.initParticlesFromSpawn(gl, entry);
        const idx = new Float32Array(this.numParticles);
        for (let i = 0; i < this.numParticles; i++) idx[i] = i;
        gl.bindBuffer(gl.ARRAY_BUFFER, this.indexBuf);
        gl.bufferData(gl.ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    }

    rebuildTerrainMesh() {
        if (!this.heightData || !this.meta) return;
        const gl = this.gl;
        const hmImg = { width: this.meta.width, height: this.meta.height };
        this.terrainMesh = buildTerrainMesh(
            this.heightData, hmImg.width, hmImg.height, this.meta, this.exaggeration);

        gl.bindBuffer(gl.ARRAY_BUFFER, this.terrainVBO);
        gl.bufferData(gl.ARRAY_BUFFER, this.terrainMesh.verts, gl.STATIC_DRAW);

        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.terrainIBO);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, this.terrainMesh.indices, gl.STATIC_DRAW);
    }

    // ── Render loop ──
    render() {
        const now = performance.now();
        const dt = (now - this._lastT) / 1000;
        this._lastT = now;

        // FPS
        this._fpsFrames++;
        this._fpsTime += dt;
        if (this._fpsTime >= 0.5) {
            this._fps = Math.round(this._fpsFrames / this._fpsTime);
            this._fpsFrames = 0;
            this._fpsTime = 0;
            document.getElementById('i-fps').textContent = this._fps;
        }

        if (this.playing) {
            this.timeFrac += dt * this.speed * 1.5;
            while (this.timeFrac >= 1.0) {
                this.timeFrac -= 1.0;
                this.currentFrame = (this.currentFrame + 1) % this.meta.n_timesteps;
                this.preload();
            }
        }

        // Update UI
        const fi = this.currentFrame;
        const tSec = this.meta.times[fi] || 0;
        document.getElementById('sl-time').value = fi;
        document.getElementById('v-time').textContent = fmtTime(tSec);
        document.getElementById('i-time').textContent = fmtTime(tSec);
        document.getElementById('i-particles').textContent =
            (this.numParticles >= 1000 ?
                Math.round(this.numParticles/1024) + 'K' : this.numParticles);

        const gl = this.gl;
        const m = this.meta;
        const curEntry = this.frameCache.get(fi);
        const nextFi = (fi + 1) % m.n_timesteps;
        const nextEntry = this.frameCache.get(nextFi) || curEntry;
        if (!curEntry) { requestAnimationFrame(() => this.render()); return; }

        const velTex = curEntry.velTex;
        const velTexNext = nextEntry.velTex;
        const spawnTex = curEntry.spawnTex;
        const spawnCount = curEntry.spawnCount;

        // Lazy-load concentration frames when needed
        if (m.has_concentration && this.showConcentration) {
            if (!this.concFrameCache.has(fi)) this.loadConcFrame(fi);
            if (!this.concFrameCache.has(nextFi)) this.loadConcFrame(nextFi);
        }

        // ============================================================
        // PASS 1: Update particle positions (2D, same as before)
        // ============================================================
        if (!this.showParticles) {
            // Clear trail FBO so no stale trails appear on terrain
            gl.bindFramebuffer(gl.FRAMEBUFFER, this.trailFB0);
            gl.viewport(0, 0, this.trailW, this.trailH);
            gl.clearColor(0, 0, 0, 0);
            gl.clear(gl.COLOR_BUFFER_BIT);
        }
        if (this.showParticles) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.particleFB1);
        gl.viewport(0, 0, this.particleRes, this.particleRes);
        gl.disable(gl.DEPTH_TEST);

        const up = this.updateProg;
        gl.useProgram(up);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(up, 'a_pos'), 2);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.particleTex0);
        gl.uniform1i(gl.getUniformLocation(up, 'u_particles'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, velTex);
        gl.uniform1i(gl.getUniformLocation(up, 'u_velocity'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, velTexNext);
        gl.uniform1i(gl.getUniformLocation(up, 'u_velocity_next'), 2);

        gl.activeTexture(gl.TEXTURE3);
        gl.bindTexture(gl.TEXTURE_2D, spawnTex);
        gl.uniform1i(gl.getUniformLocation(up, 'u_spawn_tex'), 3);
        gl.uniform1f(gl.getUniformLocation(up, 'u_spawn_count'), spawnCount);

        gl.uniform1f(gl.getUniformLocation(up, 'u_time_frac'), this.timeFrac);
        gl.uniform1f(gl.getUniformLocation(up, 'u_rand_seed'), Math.random());
        gl.uniform1f(gl.getUniformLocation(up, 'u_speed_factor'), this.speedFactor * this.speed);
        gl.uniform1f(gl.getUniformLocation(up, 'u_drop_rate'), this.dropRate);
        gl.uniform1f(gl.getUniformLocation(up, 'u_drop_rate_bump'), this.dropRateBump);
        gl.uniform2f(gl.getUniformLocation(up, 'u_vel_range_x'), m.vx_min, m.vx_max);
        gl.uniform2f(gl.getUniformLocation(up, 'u_vel_range_y'), m.vy_min, m.vy_max);
        gl.uniform2f(gl.getUniformLocation(up, 'u_grid_res'), m.width, m.height);

        gl.drawArrays(gl.TRIANGLES, 0, 6);

        [this.particleTex0, this.particleTex1] = [this.particleTex1, this.particleTex0];
        [this.particleFB0, this.particleFB1] = [this.particleFB1, this.particleFB0];

        // ============================================================
        // PASS 2: Trail accumulation in UV space
        // ============================================================
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.trailFB1);
        gl.viewport(0, 0, this.trailW, this.trailH);
        gl.disable(gl.DEPTH_TEST);

        // 2a: Fade previous trail
        const sp = this.screenProg;
        gl.useProgram(sp);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(sp, 'a_pos'), 2);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.trailTex0);
        gl.uniform1i(gl.getUniformLocation(sp, 'u_screen'), 0);
        gl.uniform1f(gl.getUniformLocation(sp, 'u_opacity'), this.fadeOpacity);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // 2b: Draw particles in UV space (additive)
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

        const dp = this.drawUvProg;
        gl.useProgram(dp);
        bindAttr(gl, this.indexBuf, gl.getAttribLocation(dp, 'a_index'), 1);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.particleTex0);
        gl.uniform1i(gl.getUniformLocation(dp, 'u_particles'), 0);
        gl.uniform1f(gl.getUniformLocation(dp, 'u_particles_res'), this.particleRes);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, velTex);
        gl.uniform1i(gl.getUniformLocation(dp, 'u_velocity'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, velTexNext);
        gl.uniform1i(gl.getUniformLocation(dp, 'u_velocity_next'), 2);

        gl.uniform1f(gl.getUniformLocation(dp, 'u_time_frac'), this.timeFrac);
        gl.uniform2f(gl.getUniformLocation(dp, 'u_vel_range_x'), m.vx_min, m.vx_max);
        gl.uniform2f(gl.getUniformLocation(dp, 'u_vel_range_y'), m.vy_min, m.vy_max);
        gl.uniform1f(gl.getUniformLocation(dp, 'u_point_size'), this.pointSize);

        gl.activeTexture(gl.TEXTURE3);
        gl.bindTexture(gl.TEXTURE_2D, this.colorRampTex);
        gl.uniform1i(gl.getUniformLocation(dp, 'u_color_ramp'), 3);

        gl.drawArrays(gl.POINTS, 0, this.numParticles);
        gl.disable(gl.BLEND);

        [this.trailTex0, this.trailTex1] = [this.trailTex1, this.trailTex0];
        [this.trailFB0, this.trailFB1] = [this.trailFB1, this.trailFB0];
        } // end if (showParticles) — passes 1 & 2

        // ============================================================
        // Shared state for all scene passes
        // ============================================================
        const cw = gl.canvas.width, ch = gl.canvas.height;
        const sunDir = [0.42, 0.78, 0.32];
        const sunLen = Math.sqrt(sunDir[0]*sunDir[0]+sunDir[1]*sunDir[1]+sunDir[2]*sunDir[2]);
        sunDir[0]/=sunLen; sunDir[1]/=sunLen; sunDir[2]/=sunLen;
        const eye = this.camera.getEye();
        const mvp = this.camera.getMVP(cw / ch);

        // Inverse VP for sky shader ray reconstruction
        const invVP = Mat4.create();
        Mat4.invert(invVP, mvp);

        // ============================================================
        // Bind scene FBO — all scene passes render here
        // ============================================================
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.sceneFBO);
        gl.viewport(0, 0, cw, ch);
        gl.clearColor(0.0, 0.0, 0.0, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

        // ── PASS SKY: Procedural sky dome ──
        gl.disable(gl.DEPTH_TEST);
        const skyP = this.skyProg;
        gl.useProgram(skyP);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(skyP, 'a_pos'), 2);
        gl.uniformMatrix4fv(gl.getUniformLocation(skyP, 'u_inv_vp'), false, invVP);
        gl.uniform3f(gl.getUniformLocation(skyP, 'u_sun_dir'), sunDir[0], sunDir[1], sunDir[2]);
        gl.uniform3f(gl.getUniformLocation(skyP, 'u_eye'), eye[0], eye[1], eye[2]);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // ── PASS TERRAIN: Dynamic-lit terrain mesh ──
        gl.enable(gl.DEPTH_TEST);

        const tp = this.terrainProg;
        gl.useProgram(tp);

        gl.bindBuffer(gl.ARRAY_BUFFER, this.terrainVBO);
        const aPos = gl.getAttribLocation(tp, 'a_position');
        const aTex = gl.getAttribLocation(tp, 'a_texcoord');
        gl.enableVertexAttribArray(aPos);
        gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 20, 0);
        gl.enableVertexAttribArray(aTex);
        gl.vertexAttribPointer(aTex, 2, gl.FLOAT, false, 20, 12);

        gl.uniformMatrix4fv(gl.getUniformLocation(tp, 'u_mvp'), false, mvp);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.hillshadeTex);
        gl.uniform1i(gl.getUniformLocation(tp, 'u_hillshade'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this.trailTex0);
        gl.uniform1i(gl.getUniformLocation(tp, 'u_trails'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, this.satelliteTex || this.hillshadeTex);
        gl.uniform1i(gl.getUniformLocation(tp, 'u_satellite'), 2);

        gl.activeTexture(gl.TEXTURE3);
        gl.bindTexture(gl.TEXTURE_2D, this.heightmapTex);
        gl.uniform1i(gl.getUniformLocation(tp, 'u_heightmap'), 3);

        gl.uniform1f(gl.getUniformLocation(tp, 'u_dark'), this.darkFactor);
        gl.uniform1f(gl.getUniformLocation(tp, 'u_use_satellite'),
            this.useSatellite && this.satelliteTex ? 1.0 : 0.0);
        gl.uniform3f(gl.getUniformLocation(tp, 'u_sun_dir'), sunDir[0], sunDir[1], sunDir[2]);
        gl.uniform3f(gl.getUniformLocation(tp, 'u_eye'), eye[0], eye[1], eye[2]);
        gl.uniform2f(gl.getUniformLocation(tp, 'u_hm_texel'), 1.0 / m.width, 1.0 / m.height);
        gl.uniform1f(gl.getUniformLocation(tp, 'u_exaggeration'), this.exaggeration);

        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.terrainIBO);
        const indexType = this.terrainMesh.useUint32 ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
        gl.drawElements(gl.TRIANGLES, this.terrainMesh.nIndices, indexType, 0);

        gl.disableVertexAttribArray(aPos);
        gl.disableVertexAttribArray(aTex);

        // ── PASS WATER: Cinematic water surface ──
        if (this.showWater && m.h_max > 0) {
            gl.enable(gl.BLEND);
            gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
            gl.depthMask(false);
            gl.enable(gl.POLYGON_OFFSET_FILL);
            gl.polygonOffset(-1.0, -1.0);

            const wp = this.waterProg;
            gl.useProgram(wp);

            gl.bindBuffer(gl.ARRAY_BUFFER, this.terrainVBO);
            const wPos = gl.getAttribLocation(wp, 'a_position');
            const wTex = gl.getAttribLocation(wp, 'a_texcoord');
            gl.enableVertexAttribArray(wPos);
            gl.vertexAttribPointer(wPos, 3, gl.FLOAT, false, 20, 0);
            gl.enableVertexAttribArray(wTex);
            gl.vertexAttribPointer(wTex, 2, gl.FLOAT, false, 20, 12);

            gl.uniformMatrix4fv(gl.getUniformLocation(wp, 'u_mvp'), false, mvp);

            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, velTex);
            gl.uniform1i(gl.getUniformLocation(wp, 'u_velocity'), 0);

            gl.activeTexture(gl.TEXTURE1);
            gl.bindTexture(gl.TEXTURE_2D, velTexNext);
            gl.uniform1i(gl.getUniformLocation(wp, 'u_velocity_next'), 1);

            gl.activeTexture(gl.TEXTURE2);
            gl.bindTexture(gl.TEXTURE_2D, this.heightmapTex);
            gl.uniform1i(gl.getUniformLocation(wp, 'u_heightmap'), 2);

            gl.uniform1f(gl.getUniformLocation(wp, 'u_time_frac'), this.timeFrac);
            gl.uniform1f(gl.getUniformLocation(wp, 'u_h_max'), m.h_max);
            gl.uniform1f(gl.getUniformLocation(wp, 'u_z_range'), m.z_max - m.z_min);
            gl.uniform1f(gl.getUniformLocation(wp, 'u_exaggeration'), this.exaggeration);

            gl.uniform1f(gl.getUniformLocation(wp, 'u_anim_time'), performance.now() / 1000.0);
            gl.uniform3f(gl.getUniformLocation(wp, 'u_eye'), eye[0], eye[1], eye[2]);
            gl.uniform2f(gl.getUniformLocation(wp, 'u_texel_size'), 1.0 / m.width, 1.0 / m.height);
            gl.uniform2f(gl.getUniformLocation(wp, 'u_vel_range_x'), m.vx_min, m.vx_max);
            gl.uniform2f(gl.getUniformLocation(wp, 'u_vel_range_y'), m.vy_min, m.vy_max);
            gl.uniform3f(gl.getUniformLocation(wp, 'u_sun_dir'), sunDir[0], sunDir[1], sunDir[2]);

            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.terrainIBO);
            gl.drawElements(gl.TRIANGLES, this.terrainMesh.nIndices, indexType, 0);

            gl.disableVertexAttribArray(wPos);
            gl.disableVertexAttribArray(wTex);
            gl.disable(gl.POLYGON_OFFSET_FILL);
            gl.disable(gl.BLEND);
            gl.depthMask(true);
        }

        // ── PASS CONCENTRATION: Overlay on water ──
        if (this.showConcentration && m.has_concentration) {
            const concTex = this.concFrameCache.get(fi);
            const concTexNext = this.concFrameCache.get(nextFi) || concTex;
            if (concTex) {
                gl.enable(gl.BLEND);
                gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
                gl.depthMask(false);
                gl.enable(gl.POLYGON_OFFSET_FILL);
                gl.polygonOffset(-2.0, -2.0);

                const cp = this.concProg;
                gl.useProgram(cp);

                gl.bindBuffer(gl.ARRAY_BUFFER, this.terrainVBO);
                const cPos = gl.getAttribLocation(cp, 'a_position');
                const cTex = gl.getAttribLocation(cp, 'a_texcoord');
                gl.enableVertexAttribArray(cPos);
                gl.vertexAttribPointer(cPos, 3, gl.FLOAT, false, 20, 0);
                gl.enableVertexAttribArray(cTex);
                gl.vertexAttribPointer(cTex, 2, gl.FLOAT, false, 20, 12);

                gl.uniformMatrix4fv(gl.getUniformLocation(cp, 'u_mvp'), false, mvp);

                gl.activeTexture(gl.TEXTURE0);
                gl.bindTexture(gl.TEXTURE_2D, concTex);
                gl.uniform1i(gl.getUniformLocation(cp, 'u_concentration'), 0);

                gl.activeTexture(gl.TEXTURE1);
                gl.bindTexture(gl.TEXTURE_2D, this.heightmapTex);
                gl.uniform1i(gl.getUniformLocation(cp, 'u_heightmap'), 1);

                gl.activeTexture(gl.TEXTURE2);
                gl.bindTexture(gl.TEXTURE_2D, velTex);
                gl.uniform1i(gl.getUniformLocation(cp, 'u_velocity'), 2);

                gl.activeTexture(gl.TEXTURE3);
                gl.bindTexture(gl.TEXTURE_2D, velTexNext);
                gl.uniform1i(gl.getUniformLocation(cp, 'u_velocity_next'), 3);

                gl.uniform1f(gl.getUniformLocation(cp, 'u_time_frac'), this.timeFrac);
                gl.uniform1f(gl.getUniformLocation(cp, 'u_h_max'), m.h_max);
                gl.uniform1f(gl.getUniformLocation(cp, 'u_z_range'), m.z_max - m.z_min);
                gl.uniform1f(gl.getUniformLocation(cp, 'u_exaggeration'), this.exaggeration);

                gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.terrainIBO);
                gl.drawElements(gl.TRIANGLES, this.terrainMesh.nIndices, indexType, 0);

                gl.disableVertexAttribArray(cPos);
                gl.disableVertexAttribArray(cTex);
                gl.disable(gl.POLYGON_OFFSET_FILL);
                gl.disable(gl.BLEND);
                gl.depthMask(true);
            }
        }

        // ── PASS PARTICLES 3D ──
        if (this.showParticles) {
        gl.depthMask(false);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE);

        const d3 = this.draw3dProg;
        gl.useProgram(d3);
        bindAttr(gl, this.indexBuf, gl.getAttribLocation(d3, 'a_index'), 1);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.particleTex0);
        gl.uniform1i(gl.getUniformLocation(d3, 'u_particles'), 0);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_particles_res'), this.particleRes);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, velTex);
        gl.uniform1i(gl.getUniformLocation(d3, 'u_velocity'), 1);

        gl.activeTexture(gl.TEXTURE2);
        gl.bindTexture(gl.TEXTURE_2D, velTexNext);
        gl.uniform1i(gl.getUniformLocation(d3, 'u_velocity_next'), 2);

        gl.activeTexture(gl.TEXTURE3);
        gl.bindTexture(gl.TEXTURE_2D, this.colorRampTex);
        gl.uniform1i(gl.getUniformLocation(d3, 'u_color_ramp'), 3);

        gl.activeTexture(gl.TEXTURE4);
        gl.bindTexture(gl.TEXTURE_2D, this.heightmapTex);
        gl.uniform1i(gl.getUniformLocation(d3, 'u_heightmap'), 4);

        gl.uniform1f(gl.getUniformLocation(d3, 'u_time_frac'), this.timeFrac);
        gl.uniform2f(gl.getUniformLocation(d3, 'u_vel_range_x'), m.vx_min, m.vx_max);
        gl.uniform2f(gl.getUniformLocation(d3, 'u_vel_range_y'), m.vy_min, m.vy_max);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_point_size'), this.pointSize);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_z_min'), m.z_min);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_z_range'), m.z_max - m.z_min);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_exaggeration'), this.exaggeration);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_aspect'), this.terrainAspect);
        gl.uniform1f(gl.getUniformLocation(d3, 'u_h_max'), m.h_max || 0);
        gl.uniformMatrix4fv(gl.getUniformLocation(d3, 'u_mvp'), false, mvp);

        gl.drawArrays(gl.POINTS, 0, this.numParticles);

        gl.disable(gl.BLEND);
        gl.depthMask(true);
        } // end if (showParticles)

        // ============================================================
        // POST-PROCESSING PIPELINE
        // ============================================================
        gl.disable(gl.DEPTH_TEST);
        gl.disable(gl.BLEND);

        // ── PASS BLOOM-EXTRACT: Extract bright pixels ──
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.brightFBO);
        gl.viewport(0, 0, this.bloomW, this.bloomH);
        const ebp = this.extractBrightProg;
        gl.useProgram(ebp);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(ebp, 'a_pos'), 2);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.sceneColorTex);
        gl.uniform1i(gl.getUniformLocation(ebp, 'u_scene'), 0);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // ── PASS BLUR-H: Horizontal Gaussian blur ──
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFBO_A);
        const blrP = this.blurProg;
        gl.useProgram(blrP);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(blrP, 'a_pos'), 2);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.brightTex);
        gl.uniform1i(gl.getUniformLocation(blrP, 'u_image'), 0);
        gl.uniform2f(gl.getUniformLocation(blrP, 'u_direction'), 1.0 / this.bloomW, 0.0);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // ── PASS BLUR-V: Vertical Gaussian blur ──
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFBO_B);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.blurTexA);
        gl.uniform1i(gl.getUniformLocation(blrP, 'u_image'), 0);
        gl.uniform2f(gl.getUniformLocation(blrP, 'u_direction'), 0.0, 1.0 / this.bloomH);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // Second blur pass for softer bloom
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFBO_A);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.blurTexB);
        gl.uniform2f(gl.getUniformLocation(blrP, 'u_direction'), 1.0 / this.bloomW, 0.0);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        gl.bindFramebuffer(gl.FRAMEBUFFER, this.blurFBO_B);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.blurTexA);
        gl.uniform2f(gl.getUniformLocation(blrP, 'u_direction'), 0.0, 1.0 / this.bloomH);
        gl.drawArrays(gl.TRIANGLES, 0, 6);

        // ── PASS COMPOSITE: bloom + tone mapping + FXAA + vignette → screen ──
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, cw, ch);
        const comp = this.compositeProg;
        gl.useProgram(comp);
        bindAttr(gl, this.quadBuf, gl.getAttribLocation(comp, 'a_pos'), 2);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.sceneColorTex);
        gl.uniform1i(gl.getUniformLocation(comp, 'u_scene'), 0);

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this.blurTexB);
        gl.uniform1i(gl.getUniformLocation(comp, 'u_bloom'), 1);

        gl.uniform2f(gl.getUniformLocation(comp, 'u_texel'), 1.0 / cw, 1.0 / ch);
        gl.uniform1f(gl.getUniformLocation(comp, 'u_bloom_strength'), 0.35);

        gl.drawArrays(gl.TRIANGLES, 0, 6);

        requestAnimationFrame(() => this.render());
    }
}

function fmtTime(s) {
    if (s < 60) return Math.round(s) + 's';
    if (s < 3600) return (s/60).toFixed(1) + ' min';
    return (s/3600).toFixed(1) + ' hr';
}

// ── Init ──
const canvas = document.getElementById('gl');
let viewer;
function resize() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(window.innerWidth * dpr);
    canvas.height = Math.round(window.innerHeight * dpr);
    canvas.style.width = window.innerWidth + 'px';
    canvas.style.height = window.innerHeight + 'px';
    if (viewer) viewer.rebuildPostFBOs();
}
resize();
window.addEventListener('resize', () => {
    resize();
});

function showError(stage, detail) {
    const el = document.getElementById('loading-overlay');
    el.className = 'load-error';
    el.style.display = 'flex';
    const hints = [
        'Check that the data/ folder is in the same directory as index.html on your server.',
        'Verify your server serves .json files with Content-Type: application/json.',
        'Open the browser developer console (F12) for more details.'
    ];
    el.innerHTML =
        '<div class="load-content">' +
        '<div class="error-title">Failed to load viewer</div>' +
        '<div>' + stage + '</div>' +
        (detail ? '<div style="margin-top:8px;color:#cc9966;">' + detail + '</div>' : '') +
        '<div class="error-hint">' + hints.join('<br>') + '</div>' +
        '</div>';
}

(async () => {
    const initTimeout = setTimeout(() => {
        showError('Loading timed out after 60 seconds.',
                  'The server may be very slow or a resource may have stalled.');
    }, 60000);
    try {
        viewer = new FloodViewer3D(canvas);
        await viewer.init();
        clearTimeout(initTimeout);
    } catch (err) {
        clearTimeout(initTimeout);
        console.error('FLUXOS viewer init failed:', err);
        showError(
            err.message || 'Unknown error during initialization',
            err.url ? ('URL: ' + err.url + (err.status ? '  (HTTP ' + err.status + ')' : '')) : ''
        );
        return;
    }

    // Wire controls
    document.getElementById('sl-time').addEventListener('input', e => {
        viewer.currentFrame = parseInt(e.target.value);
        viewer.timeFrac = 0;
        viewer.preload();
    });
    document.getElementById('btn-play').addEventListener('click', () => {
        viewer.playing = !viewer.playing;
        const b = document.getElementById('btn-play');
        b.innerHTML = viewer.playing ? '&#9646;&#9646; Pause' : '&#9654; Play';
        b.classList.toggle('active', viewer.playing);
    });
    document.getElementById('btn-reset').addEventListener('click', () => {
        viewer.currentFrame = 0;
        viewer.timeFrac = 0;
        viewer.initTrailTextures(viewer.gl);
        viewer.initParticleState(viewer.gl);
    });
    document.getElementById('btn-fs').addEventListener('click', () => {
        document.documentElement.requestFullscreen().catch(() => {});
    });
    document.getElementById('sl-speed').addEventListener('input', e => {
        viewer.speed = parseInt(e.target.value) / 100;
        document.getElementById('v-speed').textContent = viewer.speed.toFixed(1) + 'x';
    });
    document.getElementById('sl-particles').addEventListener('input', e => {
        const pw = parseInt(e.target.value);
        viewer.setParticleCount(pw);
        const n = viewer.numParticles;
        document.getElementById('v-particles').textContent =
            n >= 1024 ? Math.round(n/1024) + 'K' : n;
    });
    document.getElementById('sl-trail').addEventListener('input', e => {
        viewer.fadeOpacity = parseInt(e.target.value) / 1000;
        document.getElementById('v-trail').textContent = viewer.fadeOpacity.toFixed(2);
    });
    document.getElementById('sl-exag').addEventListener('input', e => {
        viewer.exaggeration = parseInt(e.target.value) / 1000;
        document.getElementById('v-exag').textContent = viewer.exaggeration.toFixed(3) + 'x';
        viewer.rebuildTerrainMesh();
    });
    document.getElementById('btn-sat').addEventListener('click', () => {
        viewer.useSatellite = !viewer.useSatellite;
        const b = document.getElementById('btn-sat');
        b.classList.toggle('active', viewer.useSatellite);
        b.innerHTML = viewer.useSatellite
            ? '&#127757; Hillshade' : '&#127760; Satellite';
    });
    document.getElementById('btn-water').addEventListener('click', () => {
        viewer.showWater = !viewer.showWater;
        const b = document.getElementById('btn-water');
        b.classList.toggle('active', viewer.showWater);
    });
    document.getElementById('btn-particles').addEventListener('click', () => {
        viewer.showParticles = !viewer.showParticles;
        const b = document.getElementById('btn-particles');
        b.classList.toggle('active', viewer.showParticles);
    });
    document.getElementById('btn-conc').addEventListener('click', () => {
        viewer.showConcentration = !viewer.showConcentration;
        const b = document.getElementById('btn-conc');
        b.classList.toggle('active', viewer.showConcentration);
        const cbw = document.getElementById('conc-colorbar-wrap');
        cbw.style.display = viewer.showConcentration ? '' : 'none';
    });
})();
</script>
</body>
</html>
'''


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN / CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="FLUXOS KML Exporter -- export simulation results as "
                    "KML + PNG for Google Earth with time animation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Regular mesh
  python fluxos_viewer.py --results-dir ./Results --dem ./terrain.asc --utm-zone 10

  # Triangular mesh
  python fluxos_viewer.py --results-dir ./Results_tri --dem ./terrain.asc \\
      --mesh-type triangular --utm-zone 10

  # Custom variable and colour range
  python fluxos_viewer.py --results-dir ./Results --dem ./terrain.asc \\
      --utm-zone 10 --variable velocity --clim 0 1.5

Output:
  A directory of .kml + .png files.  Open fluxos_animation.kml in Google Earth.
  Use the time slider to animate through timesteps.
""",
    )
    parser.add_argument(
        "--results-dir", required=True,
        help="Directory containing simulation output files (.txt or .vtu)",
    )
    parser.add_argument(
        "--dem", required=True,
        help="Path to ESRI ASCII Grid DEM file (.asc)",
    )
    parser.add_argument(
        "--mesh-type", choices=["regular", "triangular"], default="regular",
        help="Mesh type: 'regular' (.txt) or 'triangular' (.vtu). Default: regular",
    )
    parser.add_argument(
        "--variable", choices=["h", "velocity", "conc_SW"], default="h",
        help="Variable to visualize. Default: h (water depth)",
    )
    parser.add_argument(
        "--clim", type=float, nargs=2, metavar=("MIN", "MAX"), default=None,
        help="Colour limits for the variable (e.g. --clim 0 2.0)",
    )
    parser.add_argument(
        "--utm-zone", type=int, default=None,
        help="UTM zone number (1-60). Auto-detected from DEM if omitted.",
    )
    parser.add_argument(
        "--southern-hemisphere", action="store_true",
        help="Set if the site is in the southern hemisphere.",
    )
    parser.add_argument(
        "--dem-crs", type=str, default=None,
        help="Explicit EPSG / PROJ string for the DEM's projected CRS — "
             "use this INSTEAD of --utm-zone when the DEM is not in a "
             "WGS84 UTM zone (e.g. 'EPSG:3763' for Portugal TM06, "
             "'EPSG:25830' for ETRS89 UTM 30N). Overrides --utm-zone.",
    )
    parser.add_argument(
        "--sim-start", type=str, default=None,
        help="Simulation start date-time (ISO format, e.g. '2009-01-01T00:00:00'). "
             "Defaults to value in modset.json if found, else 2000-01-01.",
    )
    parser.add_argument(
        "--output-dir", "-o", type=str, default=None,
        help="Output directory for KML + PNG files. "
             "Default: fluxos_kml_<mesh_type>",
    )
    parser.add_argument(
        "--opacity", type=int, default=140,
        help="Water overlay opacity (0-255). Default: 140",
    )
    parser.add_argument(
        "--h-min", type=float, default=0.001,
        help="Minimum water depth to include (m). Default: 0.001",
    )
    parser.add_argument(
        "--velocity-arrows", action="store_true", default=None,
        help="Overlay velocity direction arrows on the flood map.",
    )
    parser.add_argument(
        "--no-velocity-arrows", action="store_true", default=False,
        help="Disable velocity arrows (overrides JSON config).",
    )
    parser.add_argument(
        "--arrow-spacing", type=int, default=15,
        help="Spacing between velocity arrows in pixels. Default: 15",
    )
    parser.add_argument(
        "--arrow-max-px", type=int, default=12,
        help="Maximum arrow length in pixels. Default: 12",
    )
    parser.add_argument(
        "--modset", type=str, default=None,
        help="Path to modset JSON config file (reads VELOCITY_ARROWS section).",
    )
    parser.add_argument(
        "--flow-style", choices=["arrows", "streamlines", "particles", "advected"],
        default=None,
        help="Flow visualization style: 'arrows' (classic), 'streamlines' "
             "(curved flow lines), 'particles' (animated streaks), or "
             "'advected' (earth.nullschool-style persistent particle trails "
             "with fading tails — requires --export-video mp4). "
             "Default: read from JSON config, or 'streamlines'.",
    )
    parser.add_argument(
        "--streamline-density", type=int, default=None,
        help="Spacing between streamline seed points (pixels). Default: 15",
    )
    parser.add_argument(
        "--streamline-length", type=int, default=None,
        help="Max integration steps per streamline. Default: 20",
    )
    parser.add_argument(
        "--particle-count", type=int, default=None,
        help="Number of particles per frame. Default: 2000",
    )
    parser.add_argument(
        "--sat-zoom-boost", type=int, default=0,
        help="Fetch the satellite basemap at N additional zoom levels above the "
             "auto-picked value AND upsize the warped output by 2^N. Sharpens "
             "the WebGL viewer's texture when the user zooms in on streets / "
             "buildings. Costs 4^N more tiles and larger cache. Try 2 for "
             "urban-street demos on small (<500 m) areas.",
    )
    parser.add_argument(
        "--particle-tail", type=int, default=None,
        help="Particle streak length (integration steps). Default: 8",
    )
    parser.add_argument(
        "--scale", type=int, default=3, choices=[1, 2, 3, 4, 5],
        help="Supersampling factor for PNG output. Higher = sharper but larger "
             "files. scale=3 renders at 3x DEM resolution. Default: 3",
    )

    # ── Video export ──────────────────────────────────────────
    parser.add_argument(
        "--export-video", choices=["gif", "mp4"], default=None,
        help="Export as video instead of KML.  'gif' uses Pillow (no extra "
             "deps); 'mp4' requires ffmpeg.",
    )
    parser.add_argument(
        "--video-fps", type=int, default=15,
        help="Video frame rate. Default: 15",
    )
    parser.add_argument(
        "--video-scale", type=int, default=None, choices=[1, 2, 3, 4, 5],
        help="Resolution scale for video (default: 2). Separate from "
             "--scale which controls KML export.",
    )
    parser.add_argument(
        "--video-max-frames", type=int, default=200,
        help="Max frames in video; evenly subsamples if exceeded. Default: 200",
    )
    parser.add_argument(
        "--video-output", type=str, default=None,
        help="Output path for video file.  Default: fluxos_<mesh>.<ext>",
    )

    # ── Advected particle trail options ──────────────────────────
    parser.add_argument(
        "--advected-particles", type=int, default=10000,
        help="Number of persistent particles for advected mode. Default: 10000",
    )
    parser.add_argument(
        "--advected-fade", type=float, default=0.96,
        help="Trail fade factor per frame (0.90=short, 0.99=long). Default: 0.96",
    )
    parser.add_argument(
        "--advected-dark", type=float, default=0.25,
        help="Hillshade darkening factor (0=black, 1=full bright). Default: 0.25",
    )

    # ── WebGL interactive viewer export ──────────────────────
    parser.add_argument(
        "--export-webgl", action="store_true", default=False,
        help="Export an interactive WebGL particle viewer (HTML + data files).",
    )
    parser.add_argument(
        "--webgl-output", type=str, default="fluxos_web",
        help="Output directory for WebGL viewer. Default: fluxos_web/",
    )
    parser.add_argument(
        "--webgl-step", type=int, default=5,
        help="Subsample every Nth timestep for WebGL export. Default: 5",
    )
    parser.add_argument(
        "--webgl-particles", type=int, default=65536,
        help="Default particle count in viewer. Default: 65536",
    )
    parser.add_argument(
        "--webgl-port", type=int, default=8080,
        help="Local port for the auto-started HTTP server after export. "
             "Default: 8080",
    )
    parser.add_argument(
        "--webgl-no-serve", action="store_true", default=False,
        help="Do NOT auto-start an HTTP server and open the browser after "
             "export. Useful for batch/CI runs that only need the bundle.",
    )
    parser.add_argument(
        "--sat-resolution", type=int, default=5,
        help="Satellite image resolution multiplier vs DEM. Default: 5",
    )

    args = parser.parse_args()

    # ── Validate ─────────────────────────────────────────────
    if not os.path.isfile(args.dem):
        print(f"ERROR: DEM file not found: {args.dem}")
        sys.exit(1)
    if not os.path.isdir(args.results_dir):
        print(f"ERROR: Results directory not found: {args.results_dir}")
        sys.exit(1)

    if args.output_dir is None:
        args.output_dir = f"fluxos_kml_{args.mesh_type}"

    # ── Step 1: Read DEM ─────────────────────────────────────
    print("\n[1/4] Reading DEM...")
    dem, meta = read_dem_asc(args.dem)

    # ── Step 2: Coordinate transform ─────────────────────────
    print("[2/4] Setting up coordinate transform...")
    if args.dem_crs:
        # Explicit CRS wins — needed for non-UTM grids (Portugal TM06,
        # ETRS89-LAEA, State Plane, etc.) where the auto-detect path
        # silently falls back to a wrong UTM zone.
        utm_to_ll = make_crs_to_latlon(args.dem_crs)
        print(f"  DEM CRS: {args.dem_crs}")
    else:
        if args.utm_zone is not None:
            zone = args.utm_zone
            northern = not args.southern_hemisphere
        else:
            zone, northern = detect_utm_zone(meta["xllcorner"], meta["yllcorner"])
        utm_to_ll = make_utm_to_latlon(zone, northern)
        print(f"  UTM Zone {zone}{'N' if northern else 'S'}")
    test_lon, test_lat = utm_to_ll(meta["xllcorner"], meta["yllcorner"])
    print(f"  DEM SW corner -> lat={test_lat:.6f}, lon={test_lon:.6f}")

    # ── Simulation start time ────────────────────────────────
    if args.sim_start:
        sim_start = datetime.datetime.fromisoformat(args.sim_start)
    else:
        sim_start = _read_sim_start()
    print(f"  Simulation start: {sim_start.isoformat()}")

    # ── Step 3: Read results ─────────────────────────────────
    print(f"[3/4] Reading {args.mesh_type} mesh results...")
    if args.mesh_type == "regular":
        results = read_regular_results(args.results_dir)
    else:
        results = read_triangular_results(args.results_dir)

    if not results:
        print("ERROR: No results found.")
        sys.exit(1)

    # ── Compute colour limits ────────────────────────────────
    if args.clim is not None:
        clim = tuple(args.clim)
    else:
        clim = _auto_clim(results, args.mesh_type, args.variable, args.h_min)
    print(f"  Colour range: {clim[0]:.4f} - {clim[1]:.4f}")

    # ── Flow visualisation config (JSON + CLI overrides) ────
    draw_arrows = False
    va_cfg = _read_velocity_arrows_config(args.modset)
    if va_cfg.get("STATUS", False):
        draw_arrows = True

    # Build flow_config from JSON defaults
    flow_config = {
        "MODE": va_cfg.get("MODE", "streamlines"),
        "ARROW_SPACING": va_cfg.get("ARROW_SPACING", args.arrow_spacing),
        "ARROW_MAX_PX": va_cfg.get("ARROW_MAX_PX", args.arrow_max_px),
        "STREAMLINE_DENSITY": va_cfg.get("STREAMLINE_DENSITY", 15),
        "STREAMLINE_LENGTH": va_cfg.get("STREAMLINE_LENGTH", 20),
        "PARTICLE_COUNT": va_cfg.get("PARTICLE_COUNT", 2000),
        "PARTICLE_TAIL": va_cfg.get("PARTICLE_TAIL", 8),
    }

    # CLI flags override JSON
    if args.velocity_arrows:
        draw_arrows = True
    if args.flow_style:
        draw_arrows = True
        flow_config["MODE"] = args.flow_style
    if args.no_velocity_arrows:
        draw_arrows = False
    if args.streamline_density is not None:
        flow_config["STREAMLINE_DENSITY"] = args.streamline_density
    if args.streamline_length is not None:
        flow_config["STREAMLINE_LENGTH"] = args.streamline_length
    if args.particle_count is not None:
        flow_config["PARTICLE_COUNT"] = args.particle_count
    if args.particle_tail is not None:
        flow_config["PARTICLE_TAIL"] = args.particle_tail

    # ── Step 4: Export ───────────────────────────────────────
    if args.export_webgl:
        print(f"[4/4] Exporting interactive WebGL viewer "
              f"({args.mesh_type} mesh)...")
        export_webgl(
            dem, meta, results, args.mesh_type, args.variable, clim,
            output_dir=args.webgl_output, h_min=args.h_min,
            step=args.webgl_step, n_particles=args.webgl_particles,
            utm_to_ll=utm_to_ll, sat_resolution=args.sat_resolution,
            sat_zoom_boost=args.sat_zoom_boost,
        )

        # Auto-serve + open in browser (opt-out via --webgl-no-serve).
        # WebGL fetch() is same-origin so we cannot just open the file://
        # URL directly — we need a real HTTP server.
        if not args.webgl_no_serve:
            _serve_webgl_bundle(args.webgl_output, int(args.webgl_port))
        return

    # Advected mode requires --export-video mp4
    if (flow_config and flow_config.get("MODE") == "advected"
            and not args.export_video):
        print("NOTE: Advected mode requires --export-video mp4. Adding it automatically.")
        args.export_video = "mp4"

    if args.export_video:
        vscale = args.video_scale if args.video_scale else 2
        ext = args.export_video

        # Advected mode: earth.nullschool-style particle trails
        is_advected = (flow_config and
                       flow_config.get("MODE") == "advected")
        if is_advected:
            voutput = (args.video_output or
                       f"fluxos_{args.mesh_type}_advected.mp4")
            suffix = f" @ {vscale}x"
            print(f"[4/4] Exporting advected particle trail MP4 "
                  f"({args.mesh_type} mesh{suffix})...")
            export_video_advected(
                dem, meta, results, args.mesh_type, args.variable, clim,
                voutput, opacity=args.opacity, h_min=args.h_min,
                scale=vscale, fps=args.video_fps,
                max_frames=args.video_max_frames,
                n_particles=args.advected_particles,
                fade_factor=args.advected_fade,
                dark_factor=args.advected_dark,
            )
        else:
            voutput = args.video_output or f"fluxos_{args.mesh_type}.{ext}"
            suffix = f" @ {vscale}x" if vscale > 1 else ""
            print(f"[4/4] Exporting {ext.upper()} video "
                  f"({args.mesh_type} mesh{suffix})...")
            export_video(
                dem, meta, results, args.mesh_type, args.variable, clim,
                voutput, fmt=ext, opacity=args.opacity, h_min=args.h_min,
                draw_arrows=draw_arrows, flow_config=flow_config,
                scale=vscale, fps=args.video_fps,
                max_frames=args.video_max_frames,
            )
    else:
        scale = args.scale
        suffix = f" @ {scale}x" if scale > 1 else ""
        print(f"[4/4] Exporting KML + PNG ({args.mesh_type} mesh{suffix})...")
        export_kml(
            dem, meta, results, args.mesh_type, args.variable, clim,
            utm_to_ll, sim_start, args.output_dir, args.opacity, args.h_min,
            draw_arrows=draw_arrows, flow_config=flow_config, scale=scale,
        )


def _read_velocity_arrows_config(modset_path=None):
    """Read VELOCITY_ARROWS section from modset JSON config."""
    import json
    paths = [modset_path] if modset_path else ["modset.json", "Working_example/modset.json", "bin/modset.json"]
    for path in paths:
        if path and os.path.isfile(path):
            try:
                with open(path) as f:
                    cfg = json.load(f)
                return cfg.get("VELOCITY_ARROWS", {})
            except Exception:
                pass
    return {}


def _read_sim_start():
    """Try to read SIM_DATETIME_START from modset.json."""
    import json
    for path in ["modset.json", "Working_example/modset.json", "bin/modset.json"]:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    cfg = json.load(f)
                dt_str = cfg.get("SIM_DATETIME_START", "")
                if dt_str:
                    return datetime.datetime.fromisoformat(dt_str)
            except Exception:
                pass
    return datetime.datetime(2000, 1, 1)


def _auto_clim(results, mesh_type, variable, h_min):
    """Auto-detect colour limits from the data."""
    all_vals = []
    for r in results:
        if mesh_type == "regular":
            data = r["data"]
            h = data[:, 5]
            wet = h > h_min
            if not np.any(wet):
                continue
            if variable == "h":
                vals = h[wet]
            elif variable == "velocity":
                vals = np.sqrt(data[:, 6][wet]**2 + data[:, 7][wet]**2)
            elif variable == "conc_SW":
                vals = data[:, 11][wet]
            else:
                vals = h[wet]
        else:
            cdata = r["cell_data"]
            if "h" not in cdata:
                continue
            h = cdata["h"]
            wet = h > h_min
            if not np.any(wet):
                continue
            if variable == "h":
                vals = h[wet]
            elif variable == "velocity":
                if "velocity" in cdata:
                    vel = cdata["velocity"]
                    if vel.ndim == 2:
                        vals = np.sqrt(vel[wet, 0]**2 + vel[wet, 1]**2)
                    else:
                        vals = np.abs(vel[wet])
                else:
                    vals = h[wet]
            elif variable == "conc_SW":
                vals = cdata.get("conc_SW", h)[wet]
            else:
                vals = h[wet]
        valid = vals[~np.isnan(vals)]
        if len(valid) > 0:
            all_vals.append(valid)
    if all_vals:
        all_vals = np.concatenate(all_vals)
        return (0.0, float(np.percentile(all_vals, 98)))
    return (0.0, 1.0)


def _auto_vel_scale(results, mesh_type, meta, h_min):
    """Compute a global velocity scale (95th percentile) for consistent arrows."""
    all_vmag = []
    for r in results:
        if mesh_type == "regular":
            vx, vy = _extract_velocity_regular(r["data"], meta)
            h = r["data"][:, 5]
        else:
            vx, vy = _extract_velocity_triangular(r["cell_data"])
            h = r["cell_data"].get("h", np.array([]))
        wet = h > h_min
        if np.any(wet):
            vmag = np.sqrt(vx[wet] ** 2 + vy[wet] ** 2)
            valid = vmag[vmag > 1e-6]
            if len(valid) > 0:
                all_vmag.append(valid)
    if all_vmag:
        return float(np.percentile(np.concatenate(all_vmag), 95))
    return 1.0


if __name__ == "__main__":
    main()
