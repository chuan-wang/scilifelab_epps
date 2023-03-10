#!/usr/bin/env python

DESC = """
This module contains functions used to generate worklists for the Mosquito X1
instrument Zika using sample data fetched from Illumina Clarity LIMS.

The functions are written with the intention of being re-useable for different
applications of the instrument.

Written by Alfred Kedhammar
"""

import pandas as pd
import numpy as np
from datetime import datetime as dt
import sys


def verify_step(currentStep, targets = None):
    """
    Given a LIMS step and a list of targets, check whether they match. Workflow information unfortunately needs to be excavated from the samples.

    The "targets" consist of a list of tuples, whose elements are partial string matches of a workflow and step, respectively.
    Empty strings will match any workflow or step.
    """

    if currentStep.instrument.name == "Zika":

        if not targets:
            # Instrument is correct and no workflows or steps are specified
            return True

        elif any(target_tuple[1] in currentStep.type.name and target_tuple[0] == "" for target_tuple in targets):
            # Instrument and step are correct and no workflow is specified
            return True

        else:
            # Need to check all samples match at least one ongoing workflow / step combo of the targets
            sample_bools = []
            for art in [art for art in currentStep.all_inputs() if art.type == "Analyte"]:
                active_stages = [stage_tuple for stage_tuple in art.workflow_stages_and_statuses if stage_tuple[1] == "IN_PROGRESS"]
                sample_bools.append(
                        # Sample has at least one ongoing target step in the target workflow
                        any(
                            workflow_string in active_stage[0].workflow.name and step_string in active_stage[2]
                                for active_stage in active_stages
                                    for workflow_string, step_string in targets
                        ))
                
            return all(sample_bools)
        
    else:
        return False


class CheckLog(Exception):

    def __init__(self, log, log_filename, lims, currentStep):

        write_log(log, log_filename)
        upload_log(currentStep, lims, log_filename)
        
        sys.stderr.write("ERROR: Check log for more info.")
        sys.exit(2)
        

def fetch_output_udfs(currentStep):
    outputs = [output for output in currentStep.all_outputs() if output.type == "Analyte"]
    return currentStep.all_outputs


def assert_udfs(currentStep):

    conc_or_amount_udfs = ["Target Amount (ng)", "Pool Conc. (nM)"]
    vol_udfs = ["Final Volume (uL)", "Target Total Volume (uL)"]
    
    outputs = [output for output in currentStep.all_outputs() if output.type == "Analyte"]
        
    try:
        for output in outputs:

            output_udfs = [kv[0] for kv in output.udf.items()]
            assert any([vol_udf in output_udfs for vol_udf in vol_udfs]) and \
                any([conc_or_amount_udf in output_udfs for conc_or_amount_udf in conc_or_amount_udfs]), \
                "All samples / pools need to have a specified output volume and concentration / amount"

    except AssertionError as e:
        sys.stderr.write(str(e))
        sys.exit(2)


def fetch_sample_data(currentStep, to_fetch):
    """ Given a dictionary "to_fetch" whose keys are the headers of the sought sample properties and whose values are the
    corresponding object paths, fetch the sample properties for all elements of currentStep.input_output_maps
    and return them in a dataframe.
    """

    object_paths = [
        # Input info
        "art_tuple[0]['uri'].name",                                 # Sample name
        "art_tuple[0]['uri'].samples[0].name",                      # Sample P-number, if not pool
        "art_tuple[0]['uri'].id",                                   # Sample LIMS ID
        "art_tuple[0]['uri'].location[0].name",                     # Plate name
        "art_tuple[0]['uri'].location[0].id",                       # Plate LIMS ID
        "art_tuple[0]['uri'].location[1]",                          # Well

        # Input UDFs
        "art_tuple[0]['uri'].udf['Conc. Units']",                   # ng/ul or nM
        "art_tuple[0]['uri'].udf['Concentration']",
        "art_tuple[0]['uri'].udf['Volume (ul)']",
        "art_tuple[0]['uri'].udf['Amount (ng)']",
        "art_tuple[0]['uri'].samples[0].udf['Customer Conc']",      # ng/ul
        "art_tuple[0]['uri'].samples[0].udf['Customer Volume']",

        # Output info
        "art_tuple[1]['uri'].name", 
        "art_tuple[1]['uri'].id",
        "art_tuple[1]['uri'].location[0].name",
        "art_tuple[1]['uri'].location[0].id",
        "art_tuple[1]['uri'].location[1]",

        # Output UDFs
        "art_tuple[1]['uri'].udf['Amount taken (ng)']",             # The amount (ng) that is taken from the original sample plate
        "art_tuple[1]['uri'].udf['Total Volume (uL)']",             # The total volume of dilution
        "art_tuple[1]['uri'].udf['Final Volume (uL)']",             # Final pool / sample volume
        "art_tuple[1]['uri'].udf['Target Amount (ng)']",            # In methods where the prep input is possibly different from the sample dilution, this is the target concentration and minimum volume of the prep input
        "art_tuple[1]['uri'].udf['Target Total Volume (uL)']",      # In methods where the prep input is possibly different from the sample dilution, this is the target concentration and minimum volume of the prep input
        "art_tuple[1]['uri'].udf['Pool Conc. (nM)']",

        # Input sample RC measurements (?)
        "art_tuple[0]['uri'].samples[0].artifact.udf['Conc. Units']",
        "art_tuple[0]['uri'].samples[0].artifact.udf['Concentration']",
        "art_tuple[0]['uri'].samples[0].artifact.udf['Volume (ul)']",
        "art_tuple[0]['uri'].samples[0].artifact.udf['Amount (ng)']"
    ]

    # Verify all target metrics are found in object_paths, if not - add them
    for header, object_path in to_fetch.items():
        assert object_path in object_paths, f"fetch_sample_data() is missing the requested object path {object_path}"

    # Fetch all input/output sample tuples
    art_tuples = [
        art_tuple for art_tuple in currentStep.input_output_maps
        if art_tuple[0]["uri"].type == art_tuple[1]["uri"].type == "Analyte"
    ]

    # Fetch all target data
    list_of_dicts = []
    for art_tuple in art_tuples:
        dict = {}
        for header, object_path in to_fetch.items():
            try:
                dict[header] = eval(object_path)
            except KeyError:
                dict[header] = None
        list_of_dicts.append(dict)

    # Compile to dataframe
    df = pd.DataFrame(list_of_dicts)

    return df


def format_worklist(df, deck):
    """
    - Add columns in Mosquito-intepretable format
    - Sort by dst transfer type, dst col, dst row
    """

    # Add columns for plate positions
    df["src_pos"] = df["src_name"].apply(lambda x: deck[x])
    df["dst_pos"] = df["dst_name"].apply(lambda x: deck[x])

    # Convert volumes to whole nl
    df["transfer_vol"] = round(df.transfer_vol * 1000, 0)
    df["transfer_vol"] = df["transfer_vol"].astype(int)

    # Convert well names to r/c coordinates
    df["src_row"], df["src_col"] = well2rowcol(df.src_well)
    df["dst_row"], df["dst_col"] = well2rowcol(df.dst_well)

    # Sort df
    try:
        # Normalization, buffer first, work column-wise dst
        df.sort_values(by = ["src_type", "dst_col", "dst_row"], inplace = True)
    except KeyError:
        # Pooling, sort by column-wise dst (pool), then by descending transfer volume
        df.sort_values(by = ["dst_col", "dst_row", "transfer_vol"], ascending = [True, True, False], inplace = True)
    df.reset_index(inplace = True, drop = True)

    # Split >5000 nl transfers

    assert all(df.transfer_vol < 180000), "Some transfer volumes exceed 180 ul"
    max_vol = 5000
    df_split = pd.DataFrame(columns = df.columns)
    # Iterate across rows
    for idx, row in df.iterrows():
        
        # If transfer volume of current row exceeds max
        if row.transfer_vol > max_vol:
            
            df_being_split = pd.DataFrame(columns = df.columns)

            # Make a copy of the row and set the transfer volume to the max
            row_cp = row.copy()
            row_cp.loc["transfer_vol"] = max_vol

            # As long as the transfer volume of the current row exceeds max
            while row.transfer_vol > max_vol:
                # Append the copy row whose transfer volume is the max
                df_being_split = df_being_split.append(row_cp)
                # Deduct the same transfer volume from the current row
                row.transfer_vol -= max_vol
            
            try:
                # If there are both sample and buffer transfers and current transfer is sample
                if row.src_type == "sample":
                    df_split = df_split.append(row)
                    df_split = df_split.append(df_being_split)
                else:
                    df_split = df_split.append(df_being_split)
                    df_split = df_split.append(row)
            except AttributeError:
                df_split = df_split.append(df_being_split)
                df_split = df_split.append(row)
                
        else:
            df_split = df_split.append(row)

    df_split.sort_index(inplace=True)
    df_split.reset_index(inplace = True, drop = True)

    return df_split


class VolumeOverflow(Exception):
    pass


def resolve_buffer_transfers(
    df = None, 
    wl_comments = None,
    buffer_strategy = "adaptive",
    well_dead_vol = 5, 
    well_max_vol = 180,
    zika_max_vol = 5
    ):
    """
    Melt buffer and sample information onto separate rows to
    produce a "one row <-> one transfer" dataframe.
    """

    # Pivot buffer transfers
    df.rename(columns = {"sample_vol": "sample", "buffer_vol": "buffer"}, inplace = True)
    to_pivot = ["sample", "buffer"]
    to_keep = ["src_name", "src_well", "dst_name", "dst_well"]
    df = df.melt(
        value_vars=to_pivot,
        var_name="src_type",
        value_name="transfer_vol",
        id_vars=to_keep,
    )
    
    # Sort df
    split_dst_well = df.dst_well.str.split(":", expand = True)
    df["dst_well_row"] = split_dst_well[0]
    df["dst_well_col"] = split_dst_well[1]

    df.sort_values(by = ["src_type", "dst_well_col", "dst_well_row"], inplace = True)

    # Remove zero-vol transfers
    df = df[df.transfer_vol > 0]

    # Re-set index
    df = df.reset_index(drop=True)

    # Assign buffer transfers to buffer plate
    df.loc[df["src_type"] == "buffer", "src_name"] = "buffer_plate"

    # Assign buffer src wells

    if buffer_strategy == "first_column":
        # Keep rows, but only use column 1
        df.loc[df["src_type"] == "buffer", "src_well"] = df.loc[
            df["src_type"] == "buffer", "src_well"
        ].apply(lambda x: x[0:-1] + "1")
        
    elif buffer_strategy == "adaptive":

        df_buffer = df[df.src_type == "buffer"]

        # Make well iterator
        wells = []
        for col in range(1,13):
            for row in list("ABCDEFGH"):
                wells.append(f"{row}:{col}")
        well_iter = iter(wells)

        # Start "filling up" buffer wells based on transfer list
        try:
            # Start at first well
            current_well = next(well_iter)
            current_well_vol = well_dead_vol

            for idx, row in df_buffer.iterrows():
                # How many subtransfers will be needed?
                n_transfers = (row.transfer_vol // zika_max_vol) + 1
                # Estimate 0.2 ul loss per transfer due to overaspiration
                vol_to_add = row.transfer_vol + 0.2 * n_transfers

                # TODO support switching buffer wells in the middle of subtransfer block
                if current_well_vol + vol_to_add > well_max_vol:
                    # Start on the next well
                    current_well = next(well_iter)
                    current_well_vol = well_dead_vol

                current_well_vol += vol_to_add
                df.loc[idx, "src_well"] = current_well

        except StopIteration:
            raise AssertionError("Total buffer volume exceeds plate capacity.")
        
        wl_comments.append(f"Fill up the buffer plate column-wise up to well {current_well} with {well_max_vol} uL buffer.")
    
    else:
        raise Exception("No buffer strategy defined")

    return df, wl_comments


def well2rowcol(well_iter):
    """
    Translates iterable of well names to list of row/column integer tuples to specify
    well location in Mosquito worklists.
    """

    # In an advanced worklist: startcol, endcol, row
    rows = []
    cols = []
    for well in well_iter:
        [row_letter, col_number] = str.split(well, sep=":")
        rowdict = {}
        for l, n in zip("ABCDEFGH", "12345678"):
            rowdict[l] = n
        rows.append(rowdict[row_letter])
        cols.append(col_number)
    return rows, cols


def get_filenames(method_name, pid):

    timestamp = dt.now().strftime("%y%m%d_%H%M%S")

    wl_filename = "_".join(["zika_worklist", method_name, pid, timestamp]) + ".csv"
    log_filename = "_".join(["zika_log", method_name, pid, timestamp]) + ".log"

    return wl_filename, log_filename


def write_worklist(df, deck, wl_filename, comments=None, multi_aspirate=None, keep_buffer_tips=None):
    """
    Write a Mosquito-interpretable advanced worklist.

    multi_aspirate -- If a buffer transfer is followed by a sample transfer
                      to the same well, and the sum of their volumes
                      is <= 5000 nl, use multi-aspiration.
    
    keep_buffer_tips -- For consecutive buffer transfers to a clean well, don't change tips

    # TODO additional tips may be saved by omitting tip changes when doing multiple transfers from a
    sample well to its normalization well

    """

    # Replace all commas with semi-colons, so they can be printed without truncating the worklist
    for c, is_string in zip(df.columns, df.applymap(type).eq(str).all()):
        if is_string:
            df[c] = df[c].apply(lambda x: x.replace(",",";"))

    # Format comments for printing into worklist
    if comments:
        comments = ["COMMENT, " + e for e in comments]

    # Default transfer type is simple copy
    df["transfer_type"] = "COPY"
    if multi_aspirate:
        filter = np.all(
            [
                # Use multi-aspirate IF...

                # End position of next transfer is the same
                df.dst_pos == df.shift(-1).dst_pos,
                # End well of the next transfer is the same
                df.dst_well == df.shift(-1).dst_well,
                # This transfer is buffer
                df.src_name == "buffer_plate",
                # Next transfer is not buffer
                df.shift(-1).src_name != "buffer_plate",
                # Sum of this and next transfer is <= 5 ul
                df.transfer_vol + df.shift(-1).transfer_vol <= 5000,
            ],
            axis=0,
        )
        df.loc[filter, "transfer_type"] = "MULTI_ASPIRATE"

    # PRECAUTION Keep tip change strategy variable definitions immutable
    tip_strats = { 
        "always": "[VAR1]",
        "never": "[VAR2]" 
    }

    # Initially, set all transfers to always change tips
    df["tip_strat"] = tip_strats["always"]

    if keep_buffer_tips:

        # Keep tips between buffer transfers
        filter = np.all(
            [
                # Keep tips IF...

                # End position of next transfer is the same
                df.dst_pos == df.shift(-1).dst_pos,
                # End well of the next transfer is the same
                df.dst_well == df.shift(-1).dst_well,
                # This transfer is buffer
                df.src_name == "buffer_plate"
            ],
            axis=0,
        )
        df.loc[filter, "tip_strat"] = tip_strats["never"]

        for i, r in df.iterrows():
            # Drop nonsense columns from multiaspirate row
            if r.transfer_type == "MULTI_ASPIRATE":
                df.loc[i, ["tip_strat", "dst_pos", "dst_col", "dst_row", "dst_name", "dst_well", "dst_well_row", "dst_well_col"]] = np.nan
            # Keep tips BEFORE multiaspirate transfer block and change tips AFTER
            elif i>1 and df.loc[i-1,"transfer_type"] == "MULTI_ASPIRATE":
                df.loc[i, "tip_strat"] = tip_strats["never"]
                new_row = {"transfer_type": "CHANGE_PIPETTES"}
                df.loc[i+0.5] = new_row
        df.sort_index(inplace=True)
        df.reset_index(inplace=True, drop=True)
            
    # Convert all data to strings
    for c in df:
        df.loc[:, c] = df[c].apply(str)

    # Write worklist
    with open(wl_filename, "w") as wl:

        wl.write("worklist,\n")

        # Define variables
        variable_definitions = []
        for tip_strat in [tip_strat for tip_strat in tip_strats.items() if tip_strat[1] in df.tip_strat.unique()]:
            variable_definitions.append(f"{tip_strat[1]}TipChangeStrategy")
            variable_definitions.append(tip_strat[0])
        wl.write(",".join(variable_definitions) + "\n")

        # Write header
        wl.write(f"COMMENT, This is the worklist {wl_filename}\n")
        if comments:
            for line in comments:
                wl.write(line + "\n")
        wl.write(get_deck_comment(deck))

        # Write transfers
        for i, r in df.iterrows():    
            if r.transfer_type == "COPY":
                wl.write(
                    ",".join(
                        [
                            r.transfer_type,
                            r.src_pos,
                            r.src_col,
                            r.src_col,
                            r.src_row,
                            r.dst_pos,
                            r.dst_col,
                            r.dst_row,
                            r.transfer_vol,
                            r.tip_strat,
                        ]
                    )
                    + "\n"
                )
            elif r.transfer_type == "MULTI_ASPIRATE":
                wl.write(
                    ",".join(
                        [
                            r.transfer_type,
                            r.src_pos,
                            r.src_col,
                            r.src_row,
                            "1",
                            r.transfer_vol,
                        ]
                    )
                    + "\n"
                )
            elif r.transfer_type == "CHANGE_PIPETTES":
                wl.write(r.transfer_type + "\n")
            else:
                raise AssertionError("No transfer type defined")
        
        wl.write(f"COMMENT, Done")


def get_deck_comment(deck):
    """ Convert the plate:position 'decktionary' into a worklist comment
    """

    pos2plate = dict([(pos, plate) for plate, pos in deck.items()])

    l = [pos2plate[i].replace(",", "") if i in pos2plate else "[Empty]" for i in range(1, 6)]

    deck_comment = "COMMENT, Set up layout:    " + "     ".join(l) + "\n"

    return deck_comment


def write_log(log, log_filename):
    with open(log_filename, "w") as logContext:
        logContext.write("\n".join(log))


def upload_log(currentStep, lims, log_filename):
    for out in currentStep.all_outputs():
        if out.name == "Mosquito Log":
            for f in out.files:
                lims.request_session.delete(f.uri)
            lims.upload_new_file(out, log_filename)


def upload_csv(currentStep, lims, wl_filename):
    for out in currentStep.all_outputs():
        if out.name == "Mosquito CSV File":
            for f in out.files:
                lims.request_session.delete(f.uri)
            lims.upload_new_file(out, wl_filename)

