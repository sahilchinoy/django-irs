import os
import csv
import shutil
import zipfile
import logging
import requests
import probablepeople
from decimal import Decimal
from datetime import datetime
from django.conf import settings
from django.core.management.base import BaseCommand
from irs.models import F8872, Contribution, Expenditure, Committee

logger = logging.getLogger(__name__)

# These are terms in the raw data that don't actually mean anything
NULL_TERMS = [
    'N/A',
    'NOT APPLICABLE',
    'NA',
    'NONE',
    'NOT APPLICABE',
    'NOT APLICABLE',
    'N A',
    'N-A']

# Global lists of contribution and expenditure objects that we'll use
# for our bulk create
CONTRIBUTIONS = []
EXPENDITURES = []

# Whether to tag contributor names
PARSE_PEOPLE = False

# Running list of filing ids so we don't add contributions or expenditures
# without an associated filing
PARSED_FILING_IDS = set()


class RowParser:
    """
    Takes a row from the raw data and a mapping of field
    positions to field names in order to clean and save the
    row to the database.
    """

    def __init__(self, form_type, mapping, row):
        self.form_type = form_type
        self.mapping = mapping
        self.row = row
        self.parsed_row = {}

        self.parse_row()
        self.create_object()

    def clean_cell(self, cell, cell_type):
        """
        Uses the type of field (from the mapping) to
        determine how to clean and format the cell.
        """
        try:
            # Get rid of non-ASCII characters
            cell = cell.encode('ascii', 'ignore').decode()
            if cell_type == 'D':
                cell = datetime.strptime(cell, '%Y%m%d')
            elif cell_type == 'I':
                cell = int(cell)
            elif cell_type == 'N':
                cell = Decimal(cell)
            else:
                cell = cell.upper()

                if len(cell) > 50:
                    cell = cell[0:50]

                if not cell or cell in NULL_TERMS:
                    cell = None

        except:
            cell = None

        return cell

    def parse_row(self):
        """
        Parses a row, cell-by-cell, returning a dict of field names
        to the cleaned field values.
        """
        fields = self.mapping
        for i, cell in enumerate(self.row[0:len(fields)]):
            field_name, field_type = fields[str(i)]
            parsed_cell = self.clean_cell(cell, field_type)
            self.parsed_row[field_name] = parsed_cell

    def create_contribution(self):
        contribution = Contribution(**self.parsed_row)

        # If there's no filing in the database for this contribution
        if contribution.form_id_number not in PARSED_FILING_IDS:
            # Skip this contribution
            return

        contribution.filing_id = contribution.form_id_number
        contribution.committee_id = contribution.EIN

        # Probabilistic name parsing: this is not incredibly accurate
        if PARSE_PEOPLE and contribution.contributor_name:
            try:
                tagged = probablepeople.tag(contribution.contributor_name)
                entity_type = tagged[1]
                contribution.entity_type = entity_type
                if entity_type == 'Person' or entity_type == 'Household':
                    contribution.contributor_first_name = tagged[0].get(
                        'GivenName')
                    contribution.contributor_middle_name = tagged[0].get(
                        'MiddleName') or tagged[0].get('MiddleInitial')
                    contribution.contributor_last_name = tagged[0].get(
                        'Surname')
                elif entity_type == 'Corporation':
                    contribution.contributor_corporation_name = tagged[0].get(
                        'CorporationName')
            except probablepeople.RepeatedLabelError:
                pass

        CONTRIBUTIONS.append(contribution)

    def create_object(self):
        if self.form_type == 'A':
            self.create_contribution()
        elif self.form_type == 'B':
            expenditure = Expenditure(**self.parsed_row)

            # If there's no filing in the database for this expenditure
            if expenditure.form_id_number not in PARSED_FILING_IDS:
                # Skip this expenditure
                return

            expenditure.filing_id = expenditure.form_id_number
            expenditure.committee_id = expenditure.EIN

            EXPENDITURES.append(expenditure)

        elif self.form_type == '2':
            filing = F8872(**self.parsed_row)
            PARSED_FILING_IDS.add(filing.form_id_number)
            logger.debug('Parsing filing {}'.format(filing.form_id_number))
            committee, created = Committee.objects.get_or_create(
                EIN=filing.EIN)
            if created:
                committee.name = filing.organization_name
                committee.save()
            filing.committee = committee

            filing.save()


class Command(BaseCommand):

    help = "Download the latest IRS filings and load them into the database"

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            dest='test',
            default=False,
            help='Use a subset of data for testing',
        )
        parser.add_argument(
            '--backup',
            action='store_true',
            dest='backup',
            default=False,
            help='Use an (outdated) backup archived in S3',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            dest='verbose',
            default=False,
            help='More logging messages',
        )
        parser.add_argument(
            '--people',
            action='store_true',
            dest='pp',
            default=False,
            help='Use probabilistic name parsing (slow)',
        )

    def handle(self, *args, **options):
        # Configure the logger
        FORMAT = '%(asctime)s %(levelname)s: %(message)s'
        if options['verbose']:
            logging.basicConfig(
                format=FORMAT,
                datefmt='%I:%M:%S',
                level=logging.DEBUG)
        else:
            logging.basicConfig(
                format=FORMAT,
                datefmt='%I:%M:%S',
                level=logging.INFO)

        # Create a temporary data directory
        self.data_dir = os.path.join(
            settings.BASE_DIR,
            'data')
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)
        # Where to download the raw zipped archive
        self.zip_path = os.path.join(
            self.data_dir,
            'zipped_archive.zip')
        # Where to extract the archive
        self.extract_path = os.path.join(
            self.data_dir)
        # Where to store the data file
        self.final_path = os.path.join(
            self.data_dir,
            'FullDataFile.txt')

        if options['test']:
            logger.info('Using test data file')
            self.final_path = os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(__file__))),
                'tests',
                'TestDataFile.txt')
        elif options['backup']:
            logger.info('Using backup file')
            self.download_from_backup()
        else:
            logger.info('Downloading latest archive')
            self.download()
            self.unzip()
            self.clean()

        # Check that this file actually has data in it
        if os.stat(self.final_path).st_size == 0:
            raise Exception('The file to be loaded is empty!')

        logger.info('Flushing database')
        F8872.objects.all().delete()
        Contribution.objects.all().delete()
        Expenditure.objects.all().delete()
        Committee.objects.all().delete()

        logger.info('Parsing archive')
        self.build_mappings()

        global CONTRIBUTIONS
        global EXPENDITURES
        global PARSE_PEOPLE

        if options['pp']:
            PARSE_PEOPLE = True

        with open(self.final_path, 'r') as raw_file:
            reader = csv.reader(raw_file, delimiter='|')
            for row in reader:
                if len(CONTRIBUTIONS) > 5000:
                    Contribution.objects.bulk_create(CONTRIBUTIONS)
                    CONTRIBUTIONS = []
                if len(EXPENDITURES) > 5000:
                    Expenditure.objects.bulk_create(EXPENDITURES)
                    EXPENDITURES = []

                try:
                    form_type = row[0]
                    if form_type == '2':
                        RowParser(form_type, self.mappings['F8872'], row)
                    elif form_type == 'A':
                        RowParser(form_type, self.mappings['sa'], row)
                    elif form_type == 'B':
                        RowParser(form_type, self.mappings['sb'], row)
                except IndexError:
                    pass

        logger.info('Resolving amendments')
        for filing in F8872.objects.filter(amended_report_indicator=1):
            previous_filings = F8872.objects.filter(
                committee_id=filing.EIN,
                begin_date=filing.begin_date,
                end_date=filing.end_date,
                form_id_number__lt=filing.form_id_number)

            previous_filings.update(
                is_amended=True,
                amended_by_id=filing.form_id_number)

        # Delete the data directory
        shutil.rmtree(os.path.join(self.data_dir))

    def download(self):
        """
        Download the archive from the IRS website.
        """
        url = 'http://forms.irs.gov/app/pod/dataDownload/fullData'
        r = requests.get(url, stream=True)
        with open(self.zip_path, 'wb') as f:
            # This is a big file, so we download in chunks
            for chunk in r.iter_content(chunk_size=30720):
                logger.debug('Downloading...')
                f.write(chunk)
                f.flush()

    def download_from_backup(self):
        """
        Download a backed-up (but slightly outdated) version
        of the text data file.
        """
        url = 'https://s3-us-west-1.amazonaws.com/irs-itemizer/' \
            'FullDataFile.txt'
        r = requests.get(url, stream=True)
        with open(self.final_path, 'wb') as f:
            # This is a big file, so we download in chunks
            for chunk in r.iter_content(chunk_size=30720):
                logger.debug('Downloading...')
                f.write(chunk)
                f.flush()

    def unzip(self):
        """
        Unzip the archive.
        """
        logger.info('Unzipping archive')
        with zipfile.ZipFile(self.zip_path, 'r') as zipped_archive:
            data_file = zipped_archive.namelist()[0]
            zipped_archive.extract(data_file, self.extract_path)

    def clean(self):
        """
        Get the .txt file from within the many-layered
        directory structure, then delete the directories.
        """
        logger.info('Cleaning up archive')
        shutil.move(
            os.path.join(
                self.data_dir,
                'var/IRS/data/scripts/pofd/download/FullDataFile.txt'
            ),
            self.final_path
        )

        shutil.rmtree(os.path.join(self.data_dir, 'var'))
        os.remove(self.zip_path)

    def build_mappings(self):
        """
        Uses CSV files of field names and positions for
        different filing types to load mappings into memory,
        for use in parsing different types of rows.
        """
        self.mappings = {}
        for record_type in ('sa', 'sb', 'F8872'):
            path = os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(__file__))),
                'mappings',
                '{}.csv'.format(record_type))
            mapping = {}
            with open(path, 'r') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    mapping[row['position']] = (
                        row['model_name'],
                        row['field_type'])

            self.mappings[record_type] = mapping
