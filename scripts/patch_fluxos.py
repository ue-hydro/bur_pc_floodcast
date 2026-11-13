"""Apply reviewed, exact-source changes; reject drift instead of guessing."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SAFETY_HEADER = r"""// Floodcast runtime-safety-v2, 2026-11-05. GPL-3.0-or-later.
// Explicit numeric-header, right-closed interval-mean forcing convention.
#pragma once
#include <armadillo>
#include <cmath>
#include <iostream>
#include <fstream>
#include <string>
#include <array>
#include <cctype>
#include <locale>
#include <sstream>
#include <vector>

inline std::string floodcast_trim(const std::string& text) {
    auto first = text.begin(), last = text.end();
    while (first != last && std::isspace(static_cast<unsigned char>(*first))) ++first;
    while (first != last && std::isspace(static_cast<unsigned char>(*(last - 1)))) --last;
    return std::string(first, last);
}

inline bool floodcast_csv_row(const std::string& line, std::array<double, 3>& row) {
    std::size_t start = 0;
    for (std::size_t column = 0; column < row.size(); ++column) {
        auto comma = line.find(',', start);
        if ((column < 2 && comma == std::string::npos) ||
            (column == 2 && comma != std::string::npos)) return false;
        auto token = floodcast_trim(line.substr(start, comma - start));
        if (token.empty()) return false;
        std::istringstream stream(token);
        stream.imbue(std::locale::classic());
        stream >> std::noskipws >> row[column];
        if (!stream || !stream.eof() || !std::isfinite(row[column])) return false;
        start = comma == std::string::npos ? line.size() : comma + 1;
    }
    return true;
}

inline float floodcast_read_forcing(const std::string& path, arma::fmat& target,
                                    std::ofstream& log) {
    if (path.empty()) { target.set_size(0, 3); return 0; }
    // Armadillo's CSV loader coerces missing/malformed fields to zero. Parse
    // original text first, requiring exactly three complete numeric tokens.
    std::ifstream input(path);
    std::vector<std::array<double, 3>> rows;
    std::string line;
    bool valid = input.is_open(), terminal_blank = false;
    while (valid && std::getline(input, line)) {
        if (floodcast_trim(line).empty()) { terminal_blank = true; continue; }
        std::array<double, 3> row;
        if (terminal_blank || !floodcast_csv_row(line, row)) { valid = false; break; }
        rows.push_back(row);
    }
    valid = valid && !input.bad() && rows.size() >= 3;
    arma::mat source(rows.size(), 3);
    if (valid) for (std::size_t r = 0; r < rows.size(); ++r)
        for (std::size_t c = 0; c < 3; ++c) source(r, c) = rows[r][c];
    if (valid) {
        // The first numeric row is the upstream legacy header, not a sample.
        valid = source(1, 0) == 0;
        for (arma::uword r = 1; r < source.n_rows && valid; ++r) {
            double t = source(r, 0);
            valid = t >= 0 && t <= 16777216 && std::floor(t) == t &&
                    (r == 1 || t > source(r - 1, 0));
        }
    }
    if (valid) {
        target = arma::conv_to<arma::fmat>::from(source.rows(1, source.n_rows - 1));
        valid = target.is_finite();
    }
    if (!valid) {
        std::cerr << "ERROR: invalid forcing file: " << path << std::endl;
        log << "ERROR: invalid forcing file: " << path << std::endl;
        return -1;
    }
    log << "Loaded validated forcing: " << path << std::endl;
    return target(target.n_rows - 1, 0);
}

inline bool floodcast_forcing_row(const arma::fmat& values, double time,
                                  unsigned int& row) {
    for (arma::uword r = 0; r < values.n_rows; ++r) {
        if (static_cast<double>(values(r, 0)) >= time) {
            row = static_cast<unsigned int>(r);
            return true;
        }
    }
    std::cerr << "ERROR: forcing does not cover simulation time " << time << std::endl;
    return false;
}
"""


def runtime_safety(root: Path) -> list[dict]:
    """Small, auditable guards plus the tested endpoint-forcing convention."""
    records = []

    def edit(relative: str, old: str, new: str) -> None:
        record = replace_once(root / relative, old, new)
        record["file"] = relative
        records.append(record)

    main = "src/fluxos/main.cpp"
    edit(
        main,
        "int main(int argc, char* argv[])\n{",
        """int main(int argc, char* argv[])
{
    if (argc != 2) {
        std::cerr << "Usage: fluxos /absolute/path/modset.json" << std::endl;
        return EXIT_FAILURE;
    }""",
    )
    edit(
        main,
        'logFLUXOSfile.open(dirpath + "/fluxos_run.log");',
        """logFLUXOSfile.open(dirpath + "/fluxos_run.log");
        if (!logFLUXOSfile) {
            std::cerr << "ERROR: cannot open model log" << std::endl;
            return EXIT_FAILURE;
        }""",
    )
    edit(
        main,
        "    ds.ntim = std::max(ntim_meteo,ntim_inflow); // get the max ntim",
        """    if (ntim_meteo < 0 || ntim_inflow < 0 || std::max(ntim_meteo, ntim_inflow) <= 0 || ds.print_step == 0 ||
        (!ds.meteo_file.empty() && !ds.inflow_file.empty() && ntim_meteo != ntim_inflow)) {
        std::cerr << "ERROR: invalid or inconsistent forcing duration / print step" << std::endl;
        return EXIT_FAILURE;
    }
    ds.ntim = std::max(ntim_meteo,ntim_inflow); // validated matching end times""",
    )
    edit(
        main,
        "    print_next = timstart;",
        "    ds.tim = static_cast<double>(timstart);\n    print_next = timstart;",
    )
    edit(main, "while(ds.tim <= ds.ntim)", "while(ds.tim < ds.ntim)")
    edit(
        main,
        "ds.dtfl = fmin(dtfl_local, print_next - ds.tim);",
        "ds.dtfl = fmin(fmin(dtfl_local, print_next - ds.tim), ds.ntim - ds.tim);",
    )
    edit(
        main,
        "ds.dtfl = std::fmin(ds.dtfl, std::fmax(dt_forcing, DT_ABS_MAX));",
        """ds.dtfl = std::fmin(ds.dtfl, std::fmin(dt_forcing, DT_ABS_MAX));
        if (!std::isfinite(ds.dtfl) || ds.dtfl <= 0) {
            std::cerr << "ERROR: non-positive or non-finite timestep" << std::endl;
            return EXIT_FAILURE;
        }""",
    )
    edit(
        main,
        "        if (ds.tim>=print_next)",
        "        if (ds.tim >= ds.ntim) print_next = ds.ntim;\n        if (ds.tim>=print_next)",
    )
    for indent in (20, 16):
        prefix = " " * indent
        edit(
            main,
            prefix
            + 'logFLUXOSfile << "Problem when saving the results: " << print_next;\n'
            + prefix
            + "return 0;",
            prefix
            + 'logFLUXOSfile << "Problem when saving the results: " << print_next;\n'
            + prefix
            + "return EXIT_FAILURE;",
        )

    read = "src/fluxos/read_functions.cpp"
    edit(
        read,
        "        // Compulsory data",
        """        // Floodcast: validate JSON values before upstream unsigned conversions.
        auto reject = [&](const std::string& message) {
            std::cerr << "ERROR: " << message << std::endl;
            logFLUXOSfile << "ERROR: " << message << std::endl;
            return true;
        };
        const auto& print_step = ds.master_MODSET.at("OUTPUT").at("PRINT_STEP");
        if (!print_step.is_number_integer() || print_step.get<double>() <= 0 ||
            print_step.get<double>() > 16777216)
            return reject("PRINT_STEP must be a positive integer <= 16777216");
        const auto& restart = ds.master_MODSET.at("RESTART");
        if (!restart.is_boolean() || restart.get<bool>())
            return reject("RESTART must be false; restart is not supported by this validated profile");
        if (ds.master_MODSET.contains("INFLOW_FILE")) {
            const auto& path = ds.master_MODSET.at("INFLOW_FILE").at("FILENAME");
            if (!path.is_string() || floodcast_trim(path.get<std::string>()).empty())
                return reject("declared INFLOW_FILE path must be non-empty");
        }
        if (ds.master_MODSET.contains("METEO_FILE")) {
            const auto& path = ds.master_MODSET.at("METEO_FILE");
            if (!path.is_string() || floodcast_trim(path.get<std::string>()).empty())
                return reject("declared METEO_FILE path must be non-empty");
        }
        // Compulsory data""",
    )
    edit(read, "}catch(json::type_error& e){", "}catch(json::exception& e){")
    edit(
        read,
        '#include "read_functions.h"',
        '#include "read_functions.h"\n#include "floodcast_safety.h"',
    )
    original = (root / read).read_text(encoding="utf-8")
    start, end = (
        original.index("float read_meteo("),
        original.index(
            "// ============================================================================",
            original.index("float read_inflow("),
        ),
    )
    edit(
        read,
        original[start:end],
        """float read_meteo(GlobVar& ds, std::ofstream& logFLUXOSfile)
{
    float end = floodcast_read_forcing(ds.meteo_file, *ds.meteo, logFLUXOSfile);
    ds.qmelvtotal = 0;
    if (end > 0) {
        for (arma::uword r = 1; r < ds.meteo->n_rows; ++r)
            ds.qmelvtotal += (*ds.meteo)(r,1) / (1000.*3600.*24.) *
                ((*ds.meteo)(r,0) - (*ds.meteo)(r-1,0));
    }
    return end;
}

float read_inflow(GlobVar& ds, std::ofstream& logFLUXOSfile)
{
    return floodcast_read_forcing(ds.inflow_file, *ds.inflow, logFLUXOSfile);
}

""",
    )
    # Replace both selectors in each mesh implementation; never access padded rows.
    for relative, include, triangular in [
        ("src/fluxos/common.cpp", "floodcast_safety.h", False),
        ("src/fluxos/trimesh/tri_forcing.cpp", "../floodcast_safety.h", True),
    ]:
        text = (root / relative).read_text(encoding="utf-8")
        first_include = next(
            line for line in text.splitlines() if line.startswith("#include")
        )
        edit(relative, first_include, first_include + f'\n#include "{include}"')
        for kind in ("meteo", "inflow"):
            text = (root / relative).read_text(encoding="utf-8")
            anchor = (
                f"for (unsigned int a = 0; a <= (*ds.{kind})"
                if triangular
                else f"for (a=0;a<=(*ds.{kind})"
            )
            start = text.index(anchor)
            end = text.index("}", text.index("}", start) + 1) + 1
            edit(
                relative,
                text[start:end],
                f"if (!floodcast_forcing_row(*ds.{kind}, ds.tim, {kind}_rowi)) return true;",
            )
    output = "src/fluxos/write_results.cpp"
    edit(
        output,
        "    arma::mat filedata(std::max(0,a-1), ncols_out - 1);\n    filedata = filedataR(arma::span(0,std::max(0,a-1)),arma::span(0, ncols_out - 1));",
        "    arma::mat filedata(a, ncols_out);\n    if (a > 0) filedata = filedataR.rows(0, a - 1);",
    )
    header = root / "src/fluxos/floodcast_safety.h"
    if header.exists():
        raise ValueError("Refusing to overwrite existing safety header")
    header.write_text(SAFETY_HEADER, encoding="utf-8", newline="\n")
    records.append(
        {
            "file": "src/fluxos/floodcast_safety.h",
            "before_sha256": None,
            "after_sha256": hashlib.sha256(header.read_bytes()).hexdigest(),
        }
    )
    return records


def replace_once(path: Path, old: str, new: str) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if text.count(old) != 1:
        raise ValueError(f"Patch anchor not unique in {path.name}")
    revised = text.replace(old, new, 1).encode("utf-8")
    path.write_bytes(revised)
    return {
        "file": path.name,
        "before_sha256": hashlib.sha256(raw).hexdigest(),
        "after_sha256": hashlib.sha256(revised).hexdigest(),
    }


def main() -> None:
    root, report = map(Path, sys.argv[1:])
    cmake = root / "CMakeLists.txt"
    records = []
    records.append(
        replace_once(
            cmake,
            "-O3 -march=native -mtune=native -funroll-loops -ftree-vectorize -fno-math-errno",
            "-O3 -funroll-loops -ftree-vectorize -fno-math-errno",
        )
    )
    records.append(
        replace_once(
            cmake,
            'set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -flto=auto")',
            "# Floodcast: LTO disabled to keep compilation resources bounded.",
        )
    )
    records.append(
        replace_once(
            cmake,
            'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -flto=auto")',
            "# Floodcast: use baseline target architecture, not build-host CPU.",
        )
    )
    records.extend(runtime_safety(root))
    report.write_text(
        json.dumps(
            {"patch_set": "portable-build-v1+runtime-safety-v2", "changes": records},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
