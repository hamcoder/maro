import gzip
import os
import shutil
import time
from csv import reader, writer
from typing import List

import aria2p
import pandas as pd
from yaml import safe_load

from maro.cli.data_pipeline.base import DataPipeline, DataTopology
from maro.cli.data_pipeline.utils import StaticParameter
from maro.utils.logger import CliLogger

logger = CliLogger(name=__name__)


class DataCenterPipeline(DataPipeline):
    """Generate data_center data and other necessary files for the specified topology.

    The files will be generated in ~/.maro/data/data_center.
    """

    _download_file_name = "AzurePublicDatasetLinksV2.txt"

    _vm_table_file_name = "vmtable.csv.gz"
    _raw_vm_table_file_name = "vmtable_raw.csv"

    _clean_file_name = "vmtable.csv"
    _build_file_name = "vmtable.bin"

    _meta_file_name = "vmtable.yml"

    def __init__(self, topology: str, source: str, is_temp: bool = False):
        super().__init__(scenario="data_center", topology=topology, source=source, is_temp=is_temp)

        self._vm_table_file = os.path.join(self._download_folder, self._vm_table_file_name)
        self._raw_vm_table_file = os.path.join(self._clean_folder, self._raw_vm_table_file_name)
        self._cpu_readings_file_name_list = []
        self._clean_cpu_readings_file_name_list = []

        self.aria2 = aria2p.API(
            aria2p.Client(
                host="http://localhost",
                port=6800,
                secret=""
            )
        )
        self._download_file_list = []

    def download(self, is_force: bool = False):
        # Download text with all urls.
        super().download(is_force=is_force)
        if os.path.exists(self._download_file):
            # Download vm_table and cpu_readings
            self._aria2p_download(is_force=is_force)
        else:
            logger.warning(f"Not found downloaded source file: {self._download_file}.")

    def _aria2p_download(self, is_force: bool = False) -> List[list]:
        """Read from the text file which contains urls and use aria2p to download.

        Args:
            is_force (bool): Is force or not.
        """
        logger.info_green("Downloading vmtable and cpu readings.")
        # Download parts of cpu reading files.
        num_files = 2
        # Open the txt file which contains all the required urls.
        with open(self._download_file, mode="r", encoding="utf-8") as urls:
            for remote_url in urls.read().splitlines():
                # Get the file name.
                file_name = remote_url.split('/')[-1]
                # Two kinds of required files "vmtable" and "vm_cpu_readings-" start with vm.
                if file_name.startswith("vmtable"):
                    if (not is_force) and os.path.exists(self._vm_table_file):
                        logger.info_green(f"{self._vm_table_file} already exists, skipping download.")
                    else:
                        logger.info_green(f"Downloading vmtable from {remote_url} to {self._vm_table_file}.")
                        self.aria2.add_uris(uris=[remote_url], options={'dir': f"{self._download_folder}"})

                elif file_name.startswith("vm_cpu_readings") and num_files > 0:
                    num_files -= 1
                    cpu_readings_file = os.path.join(self._download_folder, file_name)
                    self._cpu_readings_file_name_list.append(file_name)

                    if (not is_force) and os.path.exists(cpu_readings_file):
                        logger.info_green(f"{cpu_readings_file} already exists, skipping download.")
                    else:
                        logger.info_green(f"Downloading cpu_readings from {remote_url} to {cpu_readings_file}.")
                        self.aria2.add_uris(uris=[remote_url], options={'dir': f"{self._download_folder}"})

        self._check_all_download_completed()

    def _check_all_download_completed(self):
        """Check all download tasks are completed and remove the ".aria2" files."""

        while 1:
            downloads = self.aria2.get_downloads()
            if len(downloads) == 0:
                logger.info_green("Doesn't exist any pending file.")
                break

            if all([download.is_complete for download in downloads]):
                # Remove temp .aria2 files.
                self.aria2.remove(downloads)
                logger.info_green("Download finished.")
                break

            for download in downloads:
                logger.info_green(f"{download.name}, {download.status}, {download.progress:.2f}%")
            logger.info_green("-" * 60)
            time.sleep(10)

    def _unzip_file(self, original_file_name: str, raw_file_name: str):
        original_file = os.path.join(self._download_folder, original_file_name)
        if os.path.exists(original_file):
            # Unzip gz file.
            raw_file = os.path.join(self._clean_folder, raw_file_name)
            logger.info_green("Unzip start.")
            with gzip.open(original_file, mode="rb") as f_in:
                logger.info_green(
                    f"Unzip {raw_file_name} from {original_file} to {raw_file}."
                )
                with open(raw_file, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            logger.info_green("Unzip finished.")
        else:
            logger.warning(f"Not found downloaded source file: {original_file}.")

    def clean(self):
        """Unzip the csv file and process it for building binary file."""
        super().clean()
        logger.info_green("Cleaning VM data.")
        # Unzip vmtable.
        self._unzip_file(original_file_name=self._vm_table_file_name, raw_file_name=self._raw_vm_table_file_name)
        # Unzip cpu readings.
        for cpu_readings_file_name in self._cpu_readings_file_name_list:
            raw_file_name = cpu_readings_file_name.split(".")[0] + "_raw.csv"
            self._clean_cpu_readings_file_name_list.append(cpu_readings_file_name[:-3])
            self._unzip_file(original_file_name=cpu_readings_file_name, raw_file_name=raw_file_name)
        # Preprocess.
        self._preprocess()

    def _process_vm_table(self, raw_vm_table_file: str) -> pd.DataFrame:
        """Process vmtable file."""

        headers = [
            'vmid', 'subscriptionid', 'deploymentid', 'vmcreated', 'vmdeleted', 'maxcpu', 'avgcpu', 'p95maxcpu',
            'vmcategory', 'vmcorecountbucket', 'vmmemorybucket'
        ]
        required_headers = ['vmid', 'vmcreated', 'vmdeleted', 'vmcorecountbucket', 'vmmemorybucket']

        vm_table = pd.read_csv(raw_vm_table_file, header=None, index_col=False, names=headers)
        vm_table = vm_table.loc[:, required_headers]

        vm_table['vmcreated'] = pd.to_numeric(vm_table['vmcreated'], errors="coerce", downcast="integer") // 300
        vm_table['vmdeleted'] = pd.to_numeric(vm_table['vmdeleted'], errors="coerce", downcast="integer") // 300
        # Transform vmcorecount '>24' bucket to 30 and vmmemory '>64' to 70.
        vm_table = vm_table.replace({'vmcorecountbucket': '>24'}, 30)
        vm_table = vm_table.replace({'vmmemorybucket': '>64'}, 70)
        vm_table['vmcorecountbucket'] = pd.to_numeric(
            vm_table['vmcorecountbucket'], errors="coerce", downcast="integer"
        )
        vm_table['vmmemorybucket'] = pd.to_numeric(vm_table['vmmemorybucket'], errors="coerce", downcast="integer")
        vm_table.dropna(inplace=True)

        vm_table['lifetime'] = vm_table['vmdeleted'] - vm_table['vmcreated']
        vm_table = vm_table.sort_values(by='vmcreated', ascending=True)
        # Generate new id column.
        vm_table = vm_table.reset_index(drop=True)
        vm_table['new_id'] = vm_table.index + 1
        vm_id_map = vm_table.set_index('vmid')['new_id']
        # Drop the original id column.
        vm_table = vm_table.drop(['vmid'], axis=1)
        # Reorder columns.
        vm_table = vm_table[['new_id', 'vmcreated', 'vmdeleted', 'vmcorecountbucket', 'vmmemorybucket']]
        # Rename column name.
        vm_table.rename(columns={'new_id': 'vmid'}, inplace=True)

        return vm_id_map, vm_table

    def _process_cpu_readings(self, clean_cpu_readings_file: str):
        """Process cpu reading file."""
        headers = ['timestamp', 'vmid', 'mincpu', 'maxcpu', 'avgcpu']
        required_headers = ['timestamp', 'vmid', 'maxcpu']

        cpu_readings = pd.read_csv(clean_cpu_readings_file, header=None, index_col=False, names=headers)
        cpu_readings = cpu_readings.loc[:, required_headers]

        cpu_readings['timestamp'] = pd.to_numeric(cpu_readings['timestamp'], errors="coerce", downcast="integer") // 300
        cpu_readings['maxcpu'] = pd.to_numeric(cpu_readings['maxcpu'], errors="coerce", downcast="float")
        cpu_readings.dropna(inplace=True)

        return cpu_readings

    def _convert_cpu_readings_id(self, old_data_path: str, new_data_path: str, vm_id_map: pd.DataFrame):
        """Convert vmid in each cpu readings file."""
        with open(old_data_path, 'r') as f_in:
            csv_reader = reader(f_in)
            with open(new_data_path, 'w') as f_out:
                csv_writer = writer(f_out)
                for row in csv_reader:
                    row[1] = vm_id_map.loc[row[1]]
                    csv_writer.writerow(row)

    def _preprocess(self):
        logger.info_green("Reading vmtable data.")
        # Process vmtable file.
        vm_id_map, vm_table = self._process_vm_table(raw_vm_table_file=self._raw_vm_table_file)
        with open(self._clean_file, mode="w", encoding="utf-8", newline="") as f:
            vm_table.to_csv(f, index=False, header=True)

        logger.info_green("Reading cpu data.")
        # Process every cpu readings file based on the vm id from vmtable.
        for clean_cpu_readings_file_name in self._clean_cpu_readings_file_name_list:
            raw_cpu_readings_file_name = clean_cpu_readings_file_name.split(".")[0] + "_raw.csv"
            raw_cpu_readings_file = os.path.join(self._clean_folder, raw_cpu_readings_file_name)
            clean_cpu_readings_file = os.path.join(self._clean_folder, clean_cpu_readings_file_name)
            # Convert vmid.
            logger.info_green(f"Convert vm id from {raw_cpu_readings_file_name} to {clean_cpu_readings_file_name}.")
            self._convert_cpu_readings_id(
                old_data_path=raw_cpu_readings_file,
                new_data_path=clean_cpu_readings_file,
                vm_id_map=vm_id_map
            )
            # Process cpu readings file.
            logger.info_green(f"Process {clean_cpu_readings_file}.")
            cpu_readings = self._process_cpu_readings(clean_cpu_readings_file=clean_cpu_readings_file)
            with open(clean_cpu_readings_file, mode="w", encoding="utf-8", newline="") as f:
                cpu_readings.to_csv(f, index=False, header=True)


class DataCenterTopology(DataTopology):
    def __init__(self, topology: str, source: str, is_temp=False):
        super().__init__()
        self._data_pipeline["vm_data"] = DataCenterPipeline(topology=topology, source=source, is_temp=is_temp)


class DataCenterProcess:
    """Contains all predefined data topologies of data_center scenario."""

    meta_file_name = "source_urls.yml"
    meta_root = os.path.join(StaticParameter.data_root, "data_center/meta")

    def __init__(self, is_temp: bool = False):
        self.topologies = {}
        self.meta_root = os.path.expanduser(self.meta_root)
        self._meta_path = os.path.join(self.meta_root, self.meta_file_name)

        with open(self._meta_path) as fp:
            self._conf = safe_load(fp)
            for topology in self._conf["vm_data"].keys():
                self.topologies[topology] = DataCenterTopology(
                    topology=topology,
                    source=self._conf["vm_data"][topology]["remote_url"],
                    is_temp=is_temp
                )