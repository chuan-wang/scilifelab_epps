#!/usr/bin/env python

import json
import logging
import os
import re
import shutil
from argparse import ArgumentParser, Namespace
from datetime import datetime as dt
from zipfile import ZipFile

import pandas as pd
from genologics.config import BASEURI, PASSWORD, USERNAME
from genologics.entities import Process
from genologics.lims import Lims
from Levenshtein import hamming as distance

from data.Chromium_10X_indexes import Chromium_10X_indexes
from scilifelab_epps.epp import upload_file
from scilifelab_epps.wrapper import epp_decorator
from scripts.generate_minknow_samplesheet import get_pool_sample_label_mapping

TIMESTAMP = dt.now().strftime("%y%m%d_%H%M%S")

# Pre-compile regexes in global scope:
IDX_PAT = re.compile("([ATCG]{4,}N*)-?([ATCG]*)")
TENX_SINGLE_PAT = re.compile("SI-(?:GA|NA)-[A-H][1-9][0-2]?")
TENX_DUAL_PAT = re.compile("SI-(?:TT|NT|NN|TN|TS)-[A-H][1-9][0-2]?")
SMARTSEQ_PAT = re.compile("SMARTSEQ[1-9]?-[1-9][0-9]?[A-P]")

# Set up Element PhiX control sets, keys are options in LIMS dropdown UDF
PHIX_SETS = {
    "PhiX Control Library, Adept": {
        "nickname": "PhiX_Adept",
        "indices": [
            ("ATGTCGCTAG", "CTAGCTCGTA"),
            ("CACAGATCGT", "ACGAGAGTCT"),
            ("GCACATAGTC", "GACTACTAGC"),
            ("TGTGTCGACA", "TGTCTGACAG"),
        ],
    },
    "Cloudbreak PhiX Control Library, Elevate": {
        "nickname": "PhiX_Elevate",
        "indices": [
            ("ACGTGTAGC", "GCTAGTGCA"),
            ("CACATGCTG", "AGACACTGT"),
            ("GTACACGAT", "CTCGTACAG"),
            ("TGTGCATCA", "TAGTCGATC"),
        ],
    },
    "Cloudbreak Freestyle PhiX Control, Third Party": {
        "nickname": "PhiX_Third",
        "indices": [
            ("ATGTCGCTAG", "CTAGCTCGTA"),
            ("CACAGATCGT", "ACGAGAGTCT"),
            ("GCACATAGTC", "GACTACTAGC"),
            ("TGTGTCGACA", "TGTCTGACAG"),
        ],
    },
}

# Load SS3 indexes
SMARTSEQ3_indexes_json = (
    "/opt/gls/clarity/users/glsai/repos/scilifelab_epps/data/SMARTSEQ3_indexes.json"
)
with open(SMARTSEQ3_indexes_json) as file:
    SMARTSEQ3_INDEXES = json.loads(file.read())


def revcomp(seq: str) -> str:
    """Reverse-complement a DNA string."""
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def idxs_from_label(label: str) -> list[str | tuple[str, str]]:
    """From a LIMS reagent label, return list whose elements are
    single indices or tuples of dual index pairs.
    """

    # Initialize result
    idxs: list[str | tuple[str, str]] = []

    # Expand 10X single indexes
    if TENX_SINGLE_PAT.findall(label):
        match = TENX_SINGLE_PAT.findall(label)[0]
        for tenXidx in Chromium_10X_indexes[match]:
            idxs.append(tenXidx)
    # Case of 10X dual indexes
    elif TENX_DUAL_PAT.findall(label):
        match = TENX_DUAL_PAT.findall(label)[0]
        i7_idx = Chromium_10X_indexes[match][0]
        i5_idx = Chromium_10X_indexes[match][1]
        idxs.append((i7_idx, revcomp(i5_idx)))
    # Case of SS3 indexes
    elif SMARTSEQ_PAT.findall(label):
        match = SMARTSEQ_PAT.findall(label)[0]
        for i7_idx in SMARTSEQ3_INDEXES[match][0]:
            for i5_idx in SMARTSEQ3_INDEXES[match][1]:
                idxs.append((i7_idx, revcomp(i5_idx)))
    # NoIndex cases
    elif label.replace(",", "").upper() == "NOINDEX" or (
        label.replace(",", "").upper() == ""
    ):
        raise AssertionError("NoIndex cases not allowed.")
    # Ordinary indexes
    elif IDX_PAT.findall(label):
        match = IDX_PAT.findall(label)[0]
        if "-" in match:
            idx1, idx2 = match.split("-")
            idxs.append((idx1, revcomp(idx2)))
        else:
            idx1 = match
            idxs.append(idx1)
    else:
        raise AssertionError(f"Could not parse index from '{label}'.")
    return idxs


def get_flowcell_id(process: Process) -> str:
    """Get the Element flowcell ID from the process."""
    flowcell_ids = [
        op.container.name for op in process.all_outputs() if op.type == "Analyte"
    ]

    assert len(set(flowcell_ids)) == 1, "Expected one flowcell ID."
    flowcell_id = flowcell_ids[0]

    if "-" in flowcell_id:
        logging.warning(
            f"Container name {flowcell_id} contains a dash, did you forget to set the name of the LIMS container to the flowcell ID?"
        )

    return flowcell_id


def make_manifests(process: Process, manifest_root_name: str) -> list[tuple[str, str]]:
    """Generate multiple manifests, grouping samples by index multiplicity and length,
    adding PhiX controls of appropriate lengths as needed.
    """

    # Assert output analytes loaded on flowcell
    arts_out = [op for op in process.all_outputs() if op.type == "Analyte"]
    assert (
        len(arts_out) == 1 or len(arts_out) == 2
    ), "Expected one or two output analytes."

    # Assert lanes
    lanes = [art_out.location[1].split(":")[0] for art_out in arts_out]
    assert set(lanes) == {"1"} or set(lanes) == {
        "1",
        "2",
    }, "Expected a single-lane or dual-lane flowcell."

    # Iterate over pool / lane
    sample_rows = []
    for pool, lane in zip(arts_out, lanes):
        # Get sample-label linkage via database
        sample2label: dict[str, str] = get_pool_sample_label_mapping(pool)
        assert len(set(pool.reagent_labels)) == len(
            pool.reagent_labels
        ), "Detected non-unique reagent labels."

        # Record PhiX UDFs for each output artifact
        phix_loaded: bool = pool.udf["% phiX"] != 0
        phix_set_name = pool.udf.get("Element PhiX Set", None)
        if phix_loaded:
            assert (
                phix_set_name is not None
            ), "PhiX controls loaded but no kit specified."
        else:
            assert phix_set_name is None, "PhiX controls specified but not loaded."

        # Collect rows for each sample
        for sample in pool.samples:
            # Include project name and sequencing setup
            if sample.project:
                project = sample.project.name.replace(".", "__").replace(",", "")
                seq_setup = sample.project.udf.get("Sequencing setup", "0-0")
            else:
                project = "Control"
                seq_setup = "0-0"

            # Add row(s), depending on index type
            lims_label = sample2label[sample.name]
            for idx in idxs_from_label(lims_label):
                row = {}
                row["SampleName"] = sample.name
                if isinstance(idx, tuple):
                    row["Index1"], row["Index2"] = idx
                else:
                    row["Index1"] = idx
                    row["Index2"] = ""
                row["Lane"] = lane
                row["Project"] = project
                row["Recipe"] = seq_setup
                row["phix_loaded"] = phix_loaded
                row["phix_set_name"] = phix_set_name

                sample_rows.append(row)

    # Compile sample dataframe
    df_samples = pd.DataFrame(sample_rows)

    # Calculate index lengths for grouping
    df_samples["len_idx1"] = df_samples["Index1"].apply(len)
    df_samples["len_idx2"] = df_samples["Index2"].apply(len)

    # Group into composite dataframes and add PhiX controls w. correct length
    dfs_samples_and_controls = []
    for (len_idx1, len_idx2, lane), group in df_samples.groupby(
        ["len_idx1", "len_idx2", "Lane"]
    ):
        if group["phix_loaded"].any():
            phix_set_name = group["phix_set_name"].iloc[0]
            phix_set = PHIX_SETS[phix_set_name]

            # Add row for each PhiX index pair
            for phix_idx_pair in phix_set["indices"]:
                row = {}
                row["SampleName"] = phix_set["nickname"]
                row["Index1"] = fit_seq(phix_idx_pair[0], len_idx1)
                row["Index2"] = fit_seq(phix_idx_pair[1], len_idx2)
                row["Lane"] = group["Lane"].iloc[0]
                row["Project"] = "Control"
                row["Recipe"] = "0-0"
                row["len_idx1"] = len_idx1
                row["len_idx2"] = len_idx2

                group = pd.concat([group, pd.DataFrame([row])], ignore_index=True)

        # Collect composite dataframes
        dfs_samples_and_controls.append(group)

    # Concatenate composite dataframes
    df_samples_and_controls = pd.concat(dfs_samples_and_controls, ignore_index=True)

    # Check for index collision per lane, across samples and PhiX
    for lane, group in df_samples_and_controls.groupby("Lane"):
        rows_to_check = group.to_dict(orient="records")
        check_distances(rows_to_check)

    # Group and make manifests
    manifests = []
    n = 0
    for (len_idx1, len_idx2, lane), group in df_samples_and_controls.groupby(
        ["len_idx1", "len_idx2", "Lane"]
    ):
        manifest_file = f"{manifest_root_name}_{n}.csv"

        runValues_section = "\n".join(
            [
                "[RUNVALUES]",
                "KeyName, Value",
                f'lims_step_name, "{process.type.name}"',
                f'lims_step_id, "{process.id}"',
                f'manifest_file, "{manifest_file}"',
                f"manifest_group, {n+1}/{len(df_samples_and_controls.groupby(['len_idx1', 'len_idx2', 'Lane']))}",
                f"grouped_by, len_idx1:{len_idx1} len_idx2:{len_idx2} lane:{lane}",
            ]
        )

        settings_section = "\n".join(
            [
                "[SETTINGS]",
                "SettingName, Value",
            ]
        )

        samples_section = (
            f"[SAMPLES]\n{group.iloc[:, 0:6].to_csv(index=None, header=True)}"
        )

        manifest_contents = "\n\n".join(
            [runValues_section, settings_section, samples_section]
        )

        manifests.append((manifest_file, manifest_contents))
        n += 1

    manifests.sort(key=lambda x: x[0])

    return manifests


def fit_seq(seq: str, length: int, seq_extension: str | None = None) -> str:
    """Fit a sequence to a given length by extending or truncating."""
    if len(seq) == length:
        return seq
    elif len(seq) > length:
        return seq[:length]
    else:
        if seq_extension is None:
            raise AssertionError("Can't extend sequence without extension string.")
        else:
            if length - len(seq) > len(seq_extension):
                raise AssertionError(
                    "Extension string too short to fit sequence to desired length."
                )
            return seq + seq_extension[: length - len(seq)]


def check_distances(rows: list[dict], dist_warning_threshold=3) -> None:
    for i in range(len(rows)):
        row = rows[i]

        for row_comp in rows[i + 1 :]:
            check_pair_distance(
                row, row_comp, dist_warning_threshold=dist_warning_threshold
            )


def check_pair_distance(
    row, row_comp, check_flips: bool = False, dist_warning_threshold: int = 3
):
    """Distance check between two index pairs.

    row                     dict   manifest row of sample A
    row_comp                dict   manifest row of sample B
    check_flips             bool   check all reverse-complement combinations
    dist_warning_threshold  int    trigger warning for distances at or below this value

    """

    if check_flips:
        flips = []
        for a1, _a1 in zip(
            [row["Index1"], revcomp(row["Index1"])], ["Index1", "Index1_rc"]
        ):
            for a2, _a2 in zip(
                [row["Index2"], revcomp(row["Index2"])], ["Index2", "Index2_rc"]
            ):
                for b1, _b1 in zip(
                    [row_comp["Index1"], revcomp(row_comp["Index1"])],
                    ["Index1", "Index1_rc"],
                ):
                    for b2, _b2 in zip(
                        [row_comp["Index2"], revcomp(row_comp["Index2"])],
                        ["Index2", "Index2_rc"],
                    ):
                        flips.append(
                            (
                                distance(a1, b1) + distance(a2, b2),
                                f"{a1}-{a2} {b1}-{b2}",
                                f"{_a1}-{_a2} {_b1}-{_b2}",
                            )
                        )
        dist, compared_seqs, flip_conf = min(flips, key=lambda x: x[0])

    else:
        dist = distance(
            row["Index1"] + row["Index2"], row_comp["Index1"] + row_comp["Index2"]
        )
        compared_seqs = (
            f"{row['Index1']}-{row['Index2']} {row_comp['Index1']}-{row_comp['Index2']}"
        )

    if dist <= dist_warning_threshold:
        # Build a warning message for the pair
        warning_lines = [
            f"Hamming distance {dist} between {row['SampleName']} and {row_comp['SampleName']}"
        ]
        # If the distance is derived from a flip, show the original and the flipped conformation
        if check_flips:
            warning_lines.append(
                f"Given: {row['Index1']}-{row['Index2']} <-> {row_comp['Index1']}-{row_comp['Index2']}"
            )
            warning_lines.append(f"Distance: {dist} when flipped to {flip_conf}")
        # If the index lengths are equal, add a simple visual representation
        if len(row["Index1"]) + len(row["Index2"]) == len(row_comp["Index1"]) + len(
            row_comp["Index2"]
        ):
            warning_lines.append(show_match(*compared_seqs.split()))

        warning = "\n".join(warning_lines)
        logging.warning(warning)

        # For identical collisions, kill the process
        if dist == 0:
            raise AssertionError("Identical indices detected.")


def show_match(seq1: str, seq2: str) -> str:
    """Visualize base-by-base match between sequences of equal length."""

    assert len(seq1) == len(seq2)

    m = ""
    for seq1_base, seq2_base in zip(seq1, seq2):
        if seq1_base == seq2_base:
            m += "|"
        else:
            m += "X"

    lines = "\n".join([seq1, m, seq2])
    return lines


@epp_decorator(script_path=__file__, timestamp=TIMESTAMP)
def main(args: Namespace):
    lims = Lims(BASEURI, USERNAME, PASSWORD)
    process = Process(lims, id=args.pid)

    # Crate manifest root name
    flowcell_id = get_flowcell_id(process)
    manifest_root_name = f"AVITI_run_manifest_{flowcell_id}_{process.id}_{TIMESTAMP}_{process.technician.name.replace(' ','')}"

    # Create manifest(s)
    files_and_contents: list[tuple[str, str]] = make_manifests(
        process, manifest_root_name
    )

    # Write manifest(s)
    for file, content in files_and_contents:
        open(file, "w").write(content)

    # Zip manifest(s)
    zip_file = f"{manifest_root_name}.zip"
    files = [file for file, _ in files_and_contents]
    with ZipFile(zip_file, "w") as zipf:
        for file in files:
            zipf.write(file)
            os.remove(file)

    # Upload manifest(s)
    logging.info("Uploading run manifest to LIMS...")
    upload_file(
        zip_file,
        args.file,
        process,
        lims,
    )

    # Move manifest(s)
    logging.info("Moving run manifest to ngi-nas-ns...")
    try:
        shutil.copyfile(
            zip_file,
            f"/srv/ngi-nas-ns/samplesheets/Aviti/{dt.now().year}/{zip_file}",
        )
        os.remove(zip_file)
    except:
        logging.error("Failed to move run manifest to ngi-nas-ns.", exc_info=True)
    else:
        logging.info("Run manifest moved to ngi-nas-ns.")


if __name__ == "__main__":
    # Parse args
    parser = ArgumentParser()
    parser.add_argument(
        "--pid",
        required=True,
        type=str,
        help="Lims ID for current Process.",
    )
    parser.add_argument(
        "--log",
        required=True,
        type=str,
        help="Which file slot to use for the script log.",
    )
    parser.add_argument(
        "--file",
        required=True,
        type=str,
        help="Which file slot to use for the run manifest.",
    )
    args = parser.parse_args()

    main(args)
