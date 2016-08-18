#!/usr/bin/env python

import argparse
import glob
import logging
import os
import pdb
import shutil
import sys
import traceback
import warnings

import gffutils
import numpy as np

import outrigger.io.star
from outrigger import util
from outrigger.index import events, adjacencies
from outrigger.io import star, gtf
from outrigger.psi import compute
from outrigger.psi.compute import MIN_READS

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import pandas as pd

OUTPUT = os.path.join('.', 'outrigger_output')
JUNCTION_PATH = os.path.join(OUTPUT, 'junctions')
JUNCTION_READS_PATH = os.path.join(JUNCTION_PATH, 'reads.csv')
JUNCTION_METADATA_PATH = os.path.join(JUNCTION_PATH, 'metadata.csv')
INDEX = os.path.join(OUTPUT, 'index')
EVENTS_CSV = 'events.csv'
METADATA_CSV = 'metadata.csv'

class CommandLine(object):
    def __init__(self, input_options=None):
        self.parser = argparse.ArgumentParser(
            description='Calculate "percent-spliced in" (Psi) scores of '
                        'alternative splicing on a *de novo*, custom-built '
                        'splicing index')

        self.subparser = self.parser.add_subparsers(help='Sub-commands')

        # --- Subcommand to build the index of splicing events --- #
        index_parser = self.subparser.add_parser(
            'index', help='Build an index of splicing events using a graph '
                          'database on your junction reads and an annotation')

        index_parser.add_argument(
            '-o', '--output', required=False, type=str, action='store',
            default=OUTPUT,
            help='Name of the folder where you saved the output from '
                 '"outrigger index" (default is ./outrigger_output, which is '
                 'relative to the directory where you called the program)". '
                 'You will need this file for the next step, "outrigger psi"'
                 ' (default="./outrigger_output")')

        junctions = index_parser.add_mutually_exclusive_group(required=True)
        junctions.add_argument(
            '-j', '--sj-out-tab', type=str, action='store',
            nargs='*', help='SJ.out.tab files from STAR aligner output')
        index_parser.add_argument('-m', '--min-reads', type=int,
                                  action='store',
                                  required=False, default=10,
                                  help='Minimum number of reads per junction '
                                       'for that junction to count in creating'
                                       ' the index of splicing events '
                                       '(default=10)')
        index_parser.add_argument('--use-multimapping', action='store_true',
                                  help='Applies to STAR SJ.out.tab files only.'
                                       ' If this flag is used, then include '
                                       'reads that mapped to multiple '
                                       'locations in the genome, not uniquely '
                                       'to a locus, in the read count for a '
                                       'junction. By default, this is off, and'
                                       ' only uniquely mapped reads are used.')

        gtf_parser = index_parser.add_mutually_exclusive_group(required=True)
        gtf_parser.add_argument('-g', '--gtf-filename', type=str,
                                action='store',
                                help="Name of the gtf file you want to use. If"
                                     " a gffutils feature database doesn't "
                                     "already exist at this location plus "
                                     "'.db' (e.g. if your gtf is gencode.v19."
                                     "annotation.gtf, then the database is "
                                     "inferred to be gencode.v19.annotation."
                                     "gtf.db), then a database will be auto-"
                                     "created. Not required if you provide a "
                                     "pre-built database with "
                                     "'--gffutils-db'")
        gtf_parser.add_argument('-d', '--gffutils-db', type=str,
                                action='store',
                                help="Name of the gffutils database file you "
                                     "want to use. The exon IDs defined here "
                                     "will be used in the function when "
                                     "creating splicing event names. Not "
                                     "required if you provide a gtf file with"
                                     " '--gtf-filename'")
        index_parser.add_argument('--debug', required=False,
                                  action='store_true',
                                  help='If given, print debugging logging '
                                       'information to standard out (Warning:'
                                       ' LOTS of output. Not recommended '
                                       'unless you think something is going '
                                       'wrong)')
        index_parser.set_defaults(func=self.index)

        # --- Subcommand to calculate psi on the built index --- #
        psi_parser = self.subparser.add_parser(
            'psi', help='Calculate "percent spliced-in" (Psi) values using '
                        'the splicing event index built with "outrigger '
                        'index"')
        psi_parser.add_argument('-i', '--index', required=False,
                                default=INDEX,
                                help='Name of the folder where you saved the '
                                     'output from "outrigger index" (default '
                                     'is {}, which is relative '
                                     'to the directory where you called this '
                                     'program, assuming you have called '
                                     '"outrigger psi" in the same folder as '
                                     'you called "outrigger index")'.format(INDEX))
        splice_junctions = psi_parser.add_mutually_exclusive_group(
            required=False)
        splice_junctions.add_argument(
            '-c', '--junction-reads-csv', required=False,
            help="Name of the splice junction files to calculate psi scores "
                 "on. If not provided, the compiled '{sj_csv}' file with all "
                 "the samples from the SJ.out.tab files that were used during "
                 "'outrigger index' will be used. Not required if you specify "
                 "SJ.out.tab file with '--sj-out-tab'".format(
                        sj_csv=JUNCTION_READS_PATH))
        splice_junctions.add_argument(
            '-j', '--sj-out-tab', required=False,
            type=str, action='store', nargs='*',
            help='SJ.out.tab files from STAR aligner output. Not required if '
                 'you specify a file with "--junction-read-csv"')
        psi_parser.add_argument('-m', '--min-reads', type=int, action='store',
                                required=False, default=10,
                                help='Minimum number of reads per junction for'
                                     ' calculating Psi (default=10)')
        psi_parser.add_argument('--use-multimapping', action='store_true',
                                help='Applies to STAR SJ.out.tab files only.'
                                     ' If this flag is used, then include '
                                     'reads that mapped to multiple '
                                     'locations in the genome, not uniquely '
                                     'to a locus, in the read count for a '
                                     'junction. By default, this is off, and'
                                     ' only uniquely mapped reads are used.')
        psi_parser.add_argument('--reads-col', default='reads',
                                help="Name of column in --splice-junction-csv "
                                     "containing reads to use. "
                                     "(default='reads')")
        psi_parser.add_argument('--sample-id-col', default='sample_id',
                                help="Name of column in --splice-junction-csv "
                                     "containing sample ids to use. "
                                     "(default='sample_id')")
        psi_parser.add_argument('--junction-id-col',
                                default='junction_id',
                                help="Name of column in --splice-junction-csv "
                                     "containing the ID of the junction to use"
                                     ". Must match exactly with the junctions "
                                     "in the index."
                                     "(default='junction_id')")
        psi_parser.add_argument('--debug', required=False, action='store_true',
                                help='If given, print debugging logging '
                                     'information to standard out')
        psi_parser.set_defaults(func=self.psi)

        if input_options is None or len(input_options) == 0:
            self.parser.print_usage()
            self.args = None
        else:
            self.args = self.parser.parse_args(input_options)

        if self.args.debug:
            print(self.args)
            print(input_options)

        self.args.func()

    def index(self):
        index = Index(**vars(self.args))
        index.execute()

    def psi(self):
        psi = Psi(**vars(self.args))
        psi.execute()

    def do_usage_and_die(self, str):
        '''Cleanly exit if incorrect parameters are given

        If a critical error is encountered, where it is suspected that the
        program is not being called with consistent parameters or data, this
        method will write out an error string (str), then terminate execution
        of the program.
        '''
        sys.stderr.write(str)
        self.parser.print_usage()
        type, value, tb = sys.exc_info()
        traceback.print_exc()
        debug = os.getenv('PYTHONDEBUG')
        if debug or self.args.debug:
            print(os.environ)
            # If the PYTHONDEBUG shell environment variable exists, then
            # launch the python debugger
            pdb.post_mortem(tb)

        return 2


# Class: Usage
class Usage(Exception):
    '''
    Used to signal a Usage error, evoking a usage statement and eventual
    exit when raised
    '''

    def __init__(self, msg):
        self.msg = msg


class Subcommand(object):

    output_folder = OUTPUT
    sj_out_tab = None
    junction_read_csv = JUNCTION_READS_PATH
    use_multimapping = False
    min_reads = MIN_READS
    gtf_filename = None
    gffutils_db = None
    debug = False

    def __init__(self, **kwargs):

        # Read all arguments and set as attributes of this class
        for key, value in kwargs.items():
            setattr(self, key, value)

        for folder in self.folders:
            self.maybe_make_folder(folder)

    def maybe_make_folder(self, folder):
        util.progress("Creating folder {} ...".format(folder))
        if not os.path.exists(folder):
            os.mkdir(folder)
        util.done()

    @property
    def folders(self):
        return self.output_folder, self.index_folder, self.gtf_folder, \
               self.junctions_folder

    @property
    def index_folder(self):
        return os.path.join(self.output_folder, 'index')

    @property
    def gtf_folder(self):
        return os.path.join(self.index_folder, 'gtf')

    @property
    def junctions_folder(self):
        return os.path.join(self.output_folder, 'junctions')

    def csv(self):
        """Create a csv file of compiled splice junctions"""

        util.progress('Reading SJ.out.files and creating a big splice junction'
                      ' table of reads spanning exon-exon junctions...')
        splice_junctions = star.read_multiple_sj_out_tab(
            self.sj_out_tab, multimapping=self.use_multimapping)
        # splice_junctions['reads'] = splice_junctions['unique_junction_reads']

        filename = self.junction_read_csv
        dirname = os.path.dirname(filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        util.progress('Writing {} ...\n'.format(filename))
        splice_junctions.to_csv(filename, index=False)
        util.done()
        return splice_junctions

    def maybe_make_db(self):
        """Get GFFutils database from file or create from a gtf"""
        if self.gffutils_db is not None:
            copied_db = os.path.join(self.gtf_folder,
                                     os.path.basename(self.gffutils_db))
            util.progress('Copying gffutils database from {} to {} '
                          '...'.format(self.gffutils_db, copied_db))
            shutil.copyfile(self.gffutils_db, copied_db)
            util.done()

            util.progress('Reading gffutils database from {} ...'.format(
                copied_db))
            db = gffutils.FeatureDB(copied_db)
            util.done()
        else:
            basename = os.path.basename(self.gtf_filename)
            db_filename = os.path.join(self.gtf_folder,  '{}.db'.format(basename))
            util.progress("Found GTF file in {} ...".format(self.gtf_filename))
            try:
                db = gffutils.FeatureDB(db_filename)
                util.progress(
                    "Found existing built outrigger-built gffutils database "
                    "file in {}".format(self.gtf_filename))
            except ValueError:
                util.progress(
                    'Creating a "gffutils" '
                    'database {}'.format(db_filename))
                db = gtf.create_db(self.gtf_filename, db_filename)
                util.done()
        return db


class Index(Subcommand):

    @staticmethod
    def junction_metadata(spliced_reads):
        """Get just the junction info from the concatenated read files"""
        util.progress('Creating splice junction metadata of merely where '
                      'junctions start and stop')
        metadata = outrigger.io.star.make_metadata(spliced_reads)
        util.done()
        return metadata

    def make_exon_junction_adjacencies(self, metadata, db):
        """Get annotated exon_cols next to junctions in data"""
        exon_junction_adjacencies = adjacencies.ExonJunctionAdjacencies(
            metadata, db)

        util.progress('Detecting de novo exons based on gaps between '
                      'junctions ...')
        exon_junction_adjacencies.detect_exons_from_junctions()
        util.done()

        novel_exons_gtf = os.path.join(self.gtf_folder, 'novel_exons.gtf')
        util.progress('Writing novel exons to {} ...'.format(novel_exons_gtf))
        exon_junction_adjacencies.write_de_novo_exons(novel_exons_gtf)
        util.done()

        util.progress('Getting junction-direction-exon triples for graph '
                      'database ...')
        junction_exon_triples = exon_junction_adjacencies.neighboring_exons()
        util.done()

        csv = os.path.join(self.index_folder,
                           'junction_exon_direction_triples.csv')
        util.progress('Writing junction-exon-direction triples'
                      ' to {}...'.format(csv))
        junction_exon_triples.to_csv(csv, index=False)
        util.done()

        return junction_exon_triples

    @staticmethod
    def make_graph(junction_exon_triples, db):
        """Create graph database of exon-junction adjacencies"""
        util.progress('Populating graph database of the '
                      'junction-direction-exon triples ...')

        event_maker = events.EventMaker(junction_exon_triples, db)
        util.done()
        return event_maker

    def make_events_by_traversing_graph(self, event_maker, db):
        for splice_name, splice_abbrev in events.SPLICE_TYPES:
            name_with_spaces = splice_name.replace('_', ' ')
            # Find event junctions
            util.progress(
                'Finding all {name} ({abbrev}) events ...'.format(
                    name=name_with_spaces, abbrev=splice_abbrev.upper()))
            events_of_type = getattr(event_maker, splice_name)()
            util.done()

            # Write to a file
            csv = os.path.join(self.index_folder, splice_abbrev.lower(),
                               EVENTS_CSV)
            dirname = os.path.dirname(csv)
            if not os.path.exists(dirname):
                os.makedirs(dirname)

            n_events = events_of_type.shape[0]
            if n_events > 0:
                util.progress(
                    'Writing {n} {abbrev} events to {csv}'
                    ' ...'.format(n=n_events, abbrev=splice_abbrev.upper(),
                                  csv=csv))
                events_of_type.to_csv(csv, index=True)
                util.done()
            else:
                util.progress(
                    'No {abbrev} events found in the junction and exon '
                    'data.'.format(abbrev=splice_abbrev.upper()))

            if n_events > 0:
                self.make_event_metadata(db, events_of_type, splice_abbrev)

    def make_event_metadata(self, db, event_df, splice_type):
        util.progress(
            'Making metadata file of {splice_type} events, '
            'annotating them with GTF attributes ...'.format(
                splice_type=splice_type.upper()))

        sa = gtf.SplicingAnnotator(db, event_df, splice_type.upper())
        util.progress('Making ".bed" files for exons in each event ...')
        folder = os.path.join(self.index_folder, splice_type)
        sa.exon_bedfiles(folder=folder)
        util.done()

        attributes = sa.attributes()
        util.done()
        util.progress('Getting exon and intron lengths of alternative '
                      'events ...')
        lengths = sa.lengths()
        util.done()
        util.progress('Combining lengths and attributes into one big '
                      'dataframe ...')
        lengths, attributes = lengths.align(attributes, axis=0, join='outer')

        metadata = pd.concat([lengths, attributes], axis=1)
        util.done()

        # Write to a file
        csv = os.path.join(self.index_folder, splice_type, METADATA_CSV)
        util.progress('Writing {splice_type} metadata to {csv} '
                      '...'.format(splice_type=splice_type.upper(), csv=csv))
        metadata.to_csv(csv, index=True, index_label=events.EVENT_ID_COLUMN)
        util.done()

    def write_new_gtf(self, db):
        gtf = os.path.join(self.gtf_folder,
                           os.path.basename(self.gtf_filename))
        util.progress('Write new GTF to {} ...'.format(gtf))
        with open(gtf, 'w') as f:
            for feature in db.all_features():
                f.write(str(feature) + '\n')
        util.done()

    def execute(self):
        # Must output the junction exon triples
        logger = logging.getLogger('outrigger.index')

        if self.debug:
            logger.setLevel(10)

        spliced_reads = self.csv()

        metadata = self.junction_metadata(spliced_reads)
        metadata_csv = os.path.join(self.junctions_folder, METADATA_CSV)
        util.progress('Writing metadata of junctions to {csv}'
                      ' ...'.format(csv=metadata_csv))
        metadata.to_csv(metadata_csv, index=False)
        util.done()

        db = self.maybe_make_db()

        junction_exon_triples = self.make_exon_junction_adjacencies(
            metadata, db)

        event_maker = self.make_graph(junction_exon_triples, db=db)
        self.make_events_by_traversing_graph(event_maker, db)

        self.write_new_gtf(db)


class Psi(Subcommand):

    # Instantiate empty variables here so PyCharm doens't get mad at me
    index = INDEX
    reads_col = None
    sample_id_col = None
    junction_id_col = None

    required_cols = {'--reads-col': reads_col,
                     '--sample-id-col': sample_id_col,
                     '--junction-id-col': junction_id_col}

    def __init__(self, **kwargs):
        # Read all arguments and set as attributes of this class
        for key, value in kwargs.items():
            setattr(self, key, value)

        if not os.path.exists(self.index_folder):
            raise OSError("The index folder ({}) doesn't exist! Cowardly "
                          "exiting because I don't know what events to "
                          "calcaulate psi on :(".format(self.index_folder))

        for splice_name, splice_folder in self.splice_type_folders.items():
            if not os.path.exists(splice_folder):
                raise OSError("The splicing index of {} ({}) splice types "
                              "doesn't exist! Cowardly existing because I "
                              "don't know how to define events :(".format(
                    splice_name, splice_folder))

        if not os.path.exists(self.junction_read_csv):
            raise OSError("The junction reads csv file ({}) doesn't exist! "
                          "Cowardly exiting because I don't have the junction "
                          "counts calcaulate psi on :(".format(
                self.junction_read_csv))

        for folder in self.folders:
            self.maybe_make_folder(folder)

    @property
    def psi_folder(self):
        return os.path.join(self.output_folder, 'psi')

    @property
    def index_folder(self):
        if not hasattr(self, 'index'):
            return os.path.join(self.output_folder, 'index')
        else:
            return self.index

    @property
    def splice_type_folders(self):
        return dict(os.path.join(self.index_folder, splice_abbrev)
                    for splice_name, splice_abbrev in events.SPLICE_TYPES)

    @property
    def folders(self):
        return self.output_folder, self.psi_folder

    def maybe_read_junction_reads(self):
        try:
            dtype = {self.reads_col: np.float32}
            if self.junction_read_csv is None:
                self.junction_read_csv = JUNCTION_READS_PATH
            util.progress(
                'Reading splice junction reads from {} ...'.format(
                    self.junction_read_csv))
            junction_reads = pd.read_csv(
                self.junction_read_csv, dtype=dtype)
            util.done()
        except OSError:
            raise IOError(
                "There is no junction reads file at the expected location"
                " ({csv}). Are you in the correct directory?".format(
                    csv=self.junction_read_csv))
        return junction_reads

    def validate_junction_reads_data(self, junction_reads):
        for flag, col in self.required_cols.items():
            if col not in junction_reads:
                raise ValueError(
                    "The required column name {col} does not exist in {csv}. "
                    "You can change this with the command line flag, "
                    "{flag}".format(col=col, csv=self.junction_read_csv,
                                    flag=flag))

    def execute(self):
        """Calculate percent spliced in (psi) of splicing events"""

        logger = logging.getLogger('outrigger.psi')
        if self.debug:
            logger.setLevel(10)

        junction_reads = self.maybe_read_junction_reads()

        junction_reads = junction_reads.set_index(
            [self.junction_id_col, self.sample_id_col])
        junction_reads.sort_index(inplace=True)
        logger.debug('\n--- Splice Junction reads ---')
        logger.debug(repr(junction_reads.head()))

        psis = []
        for splice_name, splice_abbrev in events.SPLICE_TYPES:
            filename = os.path.join(self.index_folder, splice_abbrev,
                                    EVENTS_CSV)
            # event_type = os.path.basename(filename).split('.csv')[0]
            util.progress('Reading {name} ({abbrev}) events from {filename}'
                          ' ...'.format(name=splice_name, abbrev=splice_abbrev,
                                        filename=filename))

            isoform_junctions = compute.ISOFORM_JUNCTIONS[splice_abbrev]
            event_annotation = pd.read_csv(filename, index_col=0)
            util.done()
            logger.debug('\n--- Splicing event annotation ---')
            logger.debug(repr(event_annotation.head()))

            util.progress('Calculating percent spliced-in (Psi) scores on '
                          '{name} ({abbrev}) events ...'.format(
                name=splice_name, abbrev=splice_abbrev))
            event_psi = compute.calculate_psi(
                event_annotation, junction_reads,
                min_reads=self.min_reads, debug=self.debug,
                reads_col=self.reads_col, **isoform_junctions)
            csv = os.path.join(self.psi_folder, splice_abbrev,
                               'psi.csv'.format(splice_abbrev))
            self.maybe_make_folder(os.path.dirname(csv))
            event_psi.to_csv(csv)
            psis.append(event_psi)
            util.done()

        util.progress('Concatenating all calculated psi scores '
                      'into one big matrix...')
        splicing = pd.concat(psis, axis=1)
        util.done()
        splicing = splicing.T
        csv = os.path.join(self.psi_folder, 'outrigger_psi.csv')
        util.progress('Writing a samples x features matrix of Psi '
                      'scores to {} ...'.format(csv))
        splicing.to_csv(csv)
        util.done()


def main():
    try:
        cl = CommandLine(sys.argv[1:])
    except Usage:
        cl.do_usage_and_die()


if __name__ == '__main__':
    main()
