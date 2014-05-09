#!/usr/bin/env python

from random import sample
from sys import stdout
from string import lowercase
from os.path import split, exists, splitext
from os import mkdir, remove
from collections import defaultdict

from numpy import (nonzero, array, fromstring, repeat, bitwise_or,
    uint8, zeros, arange, finfo)

from cogent import DNA
from cogent.core.alignment import DenseAlignment
from cogent.core.sequence import ModelDnaSequence
from cogent.core.profile import Profile

from skbio.parse.sequences import parse_fasta


__author__ = "Dan Knights"
__copyright__ = "Copyright 2011, The QIIME Project"
__credits__ = ["Greg Caporaso", "Justin Kuczynski", "Dan Knights"]
__license__ = "GPL"
__version__ = "1.8.0-dev"
__maintainer__ = "Dan Knights"
__email__ = "danknights@gmail.com"

"""Contains code for filtering alignments before building trees from them
"""


def mask_to_positions(maskstring):
    """Converts lanemask binary string to array of valid indices."""
    return nonzero(array(map(int, maskstring)))[0]


def get_masked_string(s, p):
    """Extracts valid positions in string s using index array p."""
    return (fromstring(s, dtype=uint8))[p].tostring()


def find_gaps(s, gapcode=45):
    """Returns index array indicating locations of gaps ('-') in string s
    """
    return nonzero(fromstring(s, dtype=uint8) == gapcode)


def apply_lane_mask(fastalines, lane_mask, verbose=False):
    """ Applies lanemask to fasta-formatted data, yielding filtered seqs.
    """
    return apply_lane_mask_and_gap_filter(fastalines, lane_mask,
                                          allowed_gap_frac=1, verbose=False)


def apply_gap_filter(fastalines, allowed_gap_frac=1 - finfo(float).eps,
                     verbose=False):
    """ Applies gap filter to fasta-formatted data, yielding filtered seqs.
    """
    return apply_lane_mask_and_gap_filter(fastalines, None,
                                          allowed_gap_frac=allowed_gap_frac, verbose=False)


def attempt_file_reset(f):
    """Attempt to seek 0"""
    if hasattr(f, 'seek'):
        f.seek(0)


def apply_lane_mask_and_gap_filter(fastalines, mask,
                                   allowed_gap_frac=1 - finfo(float).eps,
                                   verbose=False, entropy_threshold=None):
    """Applies a mask and gap filter to fasta file, yielding filtered seqs."""
    if entropy_threshold is not None and not (0 < entropy_threshold < 1):
        raise ValueError('Entropy threshold parameter (-e) needs to be '
                         'between 0 and 1')

    if mask is not None:
        mask = mask_to_positions(mask)
        prefilter_f = lambda x: get_masked_string(x, mask)
    else:
        prefilter_f = lambda x: x

    # resolve the gaps based on masked sequence
    gapcounts = None
    gapmask = slice(None)
    if allowed_gap_frac < 1:
        seq_count = 0.0
        for seq_id, seq in parse_fasta(fastalines):
            seq_count += 1
            seq = seq.replace('.', '-')

            seq = prefilter_f(seq)

            if gapcounts is None:
                gapcounts = zeros(len(seq))

            gapcounts[find_gaps(seq)] += 1

        gapmask = (gapcounts / seq_count) <= allowed_gap_frac
        gapmask = mask_to_positions(gapmask)
        attempt_file_reset(fastalines)

    # resolve the entropy mask
    if entropy_threshold is not None:
        ent_mask = generate_lane_mask(fastalines, entropy_threshold, gapmask)
        ent_mask = mask_to_positions(ent_mask)
        entropy_filter_f = lambda x: get_masked_string(x, ent_mask)
        attempt_file_reset(fastalines)
    else:
        entropy_filter_f = prefilter_f

    # mask, degap, and yield
    for seq_id, seq in parse_fasta(fastalines):
        seq = seq.replace('.', '-')

        # The order in which the mask is applied depends on whether a mask is
        # specified or inferred. Specifically, if a precomputed mask is
        # provided (e.g., the Lane mask) then it must be applied prior to a
        # gap filter, whereas if a mask is inferred then it must be applied
        # after a gap filter.
        if mask is None:
            seq = get_masked_string(seq, gapmask)
            seq = entropy_filter_f(seq)
        else:
            seq = entropy_filter_f(seq)
            seq = get_masked_string(seq, gapmask)

        yield ">%s\n" % seq_id
        yield "%s\n" % seq


def remove_outliers(seqs, num_sigmas, fraction_seqs_for_stats=.95):
    """ remove sequences very different from the majority consensus

    given aligned seqs, will calculate a majority consensus (most common
    symbol at each position of the alignment), and average edit distance
    of each seq to that consensus.  any seq whose edit dist is > cutoff
    (roughly seq_dist > num_sigmas * (average edit dist) ) is removed
    when calculating mean and stddev edit distance, only the best
    fraction_seqs_for_stats are used

    seqs must be compatible with DenseAlignment:
    aln = DenseAlignment(data=seqs, MolType=DNA) is called
    """
    aln = DenseAlignment(data=seqs, MolType=DNA)
    cons = DenseAlignment(data=aln.majorityConsensus(), MolType=DNA)
    diff_mtx = cons.SeqData[:, 0] != aln.SeqData

    # consider only a fraction of seqs for mean, std
    seq_diffs = diff_mtx.sum(1)
    num_to_consider = round(len(seq_diffs) * fraction_seqs_for_stats)
    seq_diffs_considered_sorted = \
        seq_diffs[seq_diffs.argsort()[:num_to_consider]]
    diff_cutoff = seq_diffs_considered_sorted.mean() + \
        num_sigmas * seq_diffs_considered_sorted.std()
    # mean + e.g.: 4 sigma
    seq_idxs_to_keep = arange(len(seq_diffs))[seq_diffs <= diff_cutoff]

    filtered_aln = aln.getSubAlignment(seq_idxs_to_keep)
    return filtered_aln


def status(message, dest=stdout, overwrite=True, max_len=100):
    """Writes a status message over the current line of stdout
    """
    message = str(message)
    message_len = max(len(message), max_len)
    if overwrite:
        dest.write('\b' * (message_len + 2))
    dest.write(message[0:message_len])
    if not overwrite:
        dest.write('\n')
    dest.flush()


def generate_lane_mask(infile, entropy_threshold, existing_mask=None):
    """ Generates lane mask dynamically by calculating base frequencies

    infile: open file object for aligned fasta file
    entropy_threshold:  float value that designates the percentage of entropic
     positions to be removed, i.e., 0.10 means the 10% most entropic positions
     are removed.

    """

    base_freqs = freqs_from_aln_array(infile, existing_mask)
    uncertainty = base_freqs.columnUncertainty()
    uncertainty_sorted = sorted(uncertainty)

    cutoff_index = int(round((len(uncertainty_sorted) - 1) *
                             (1 - entropy_threshold)))

    max_uncertainty = uncertainty_sorted[cutoff_index]

    # This correction is for small datasets with a small possible number of
    # uncertainty values.
    highest_certainty = min(uncertainty_sorted)

    lane_mask = ""

    for base in uncertainty:
        if base >= max_uncertainty and base != highest_certainty:
            lane_mask += "0"
        else:
            lane_mask += "1"

    return lane_mask


def freqs_from_aln_array(seqs, existing_mask=None):
    """Returns per-position freqs from arbitrary size alignment.

    Warning: fails if all seqs aren't the same length.
    written by Rob Knight

    seqs = list of lines from aligned fasta file
    """
    result = None
    for label, seq in parse_fasta(seqs):
        if existing_mask is not None:
            seq = get_masked_string(seq, existing_mask)

        # Currently cogent does not support . characters for gaps, converting
        # to - characters for compatability.
        seq = ModelDnaSequence(seq.replace('.', '-'))
        if result is None:
            result = zeros((len(seq.Alphabet), len(seq)), dtype=int)
            indices = arange(len(seq), dtype=int)
        result[seq._data, indices] += 1
    return Profile(result, seq.Alphabet)
