"""
find candidate site to distribute input datasets

"""

import datetime
import fnmatch
import re
import sys
import time
import uuid

from pandacommon.pandalogger.PandaLogger import PandaLogger
from pandaserver.config import panda_config
from pandaserver.dataservice.DataServiceUtils import select_scope
from pandaserver.dataservice.DDM import rucioAPI
from pandaserver.taskbuffer import JobUtils

# logger
_logger = PandaLogger().getLogger("DynDataDistributer")

def initLogger(pLogger):
    # redirect logging to parent
    global _logger
    _logger = pLogger

# files in datasets
g_files_in_ds_map = {}

class DynDataDistributer:
    # constructor
    def __init__(self, jobs, taskBuffer, siteMapper, simul=False, token=None, logger=None):
        self.jobs = jobs
        #self.taskBuffer = taskBuffer
        self.site_mapper = siteMapper
        if token is None:
            self.token = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(" ")
        else:
            self.token = token
        # use a fixed list since some clouds don't have active T2s
        self.pd2p_clouds = ["CA", "DE", "ES", "FR", "IT", "ND", "NL", "TW", "UK", "US"]
        self.simul = simul
        self.last_message = ""
        self.logger = logger

    def get_replica_locations(self, input_ds, check_used_file):
        """Get replica locations for a given dataset.

        Args:
            input_ds (str): The name of the input dataset.
            check_used_file (bool): Flag to check used file.

        Returns:
            tuple: A tuple containing the status (bool) and the result (dict or str).
        """
        # return for failure
        res_for_failure = False, {"": {"": ([], [], [], 0, False, False, 0, 0, [])}}

        # get replica locations
        if input_ds.endswith("/"):
            # container
            status, tmpRepMaps = self.get_list_dataset_replicas_in_container(input_ds)
            # get used datasets
            if status and check_used_file:
                status, tmpUsedDsList = self.get_used_datasets(tmpRepMaps)
                # remove unused datasets
                newRepMaps = {}
                for tmpKey in tmpRepMaps:
                    tmpVal = tmpRepMaps[tmpKey]
                    if tmpKey in tmpUsedDsList:
                        newRepMaps[tmpKey] = tmpVal
                tmpRepMaps = newRepMaps
        else:
            # normal dataset
            status, tmpRepMap = self.get_list_dataset_replicas(input_ds)
            tmpRepMaps = {input_ds: tmpRepMap}

        if not status:
            # failed
            self.put_log(f"failed to get replica locations for {input_ds}", "error")
            return res_for_failure

        return True, tmpRepMaps

    def get_all_sites(self):
        """Get all sites that meet certain conditions."""
        all_sites = []
        for site_name in self.site_mapper.siteSpecList:
            site_spec = self.site_mapper.siteSpecList[site_name]
            # check cloud
            if site_spec.cloud not in self.pd2p_clouds:
                continue
            # ignore test sites
            if "test" in site_name.lower():
                continue
            # analysis only
            if not site_spec.runs_analysis():
                continue
            # skip GPU
            if site_spec.isGPU():
                continue
            # skip VP
            if site_spec.use_vp(JobUtils.ANALY_PS):
                continue
            # online
            if site_spec.status not in ["online"]:
                continue
            all_sites.append(site_spec)
        return all_sites

    def get_candidate_sites(self, tmpRepMaps, prodsourcelabel, job_label, use_close_sites):
        """Get candidate sites for subscription."""
        all_site_map = self.get_all_sites()
        returnMap = {}
        cloud = "WORLD"
        for tmpDS in tmpRepMaps:
            tmpRepMap = tmpRepMaps[tmpDS]
            candSites = []
            sitesComDS = []
            sitesCompPD2P = []
            t1HasReplica = False
            t1HasPrimary = False
            nSecReplicas = 0
            candForMoU = []
            nUserSub = 0
            for tmpSiteSpec in all_site_map:
                tmp_scope_input, tmp_scope_output = select_scope(tmpSiteSpec, prodsourcelabel, job_label)
                if tmp_scope_input not in tmpSiteSpec.ddm_endpoints_input:
                    continue
                rses = tmpSiteSpec.ddm_endpoints_input[tmp_scope_input].getLocalEndPoints()
                hasReplica = False
                for tmpDQ2ID in tmpRepMap:
                    tmpStatMap = tmpRepMap[tmpDQ2ID]
                    if tmpDQ2ID in rses and tmpStatMap[0]["total"] == tmpStatMap[0]["found"] and tmpDQ2ID.endswith(
                            "DATADISK"):
                        sitesComDS.append(tmpSiteSpec.sitename)
                        hasReplica = True
                        break
                if hasReplica or not use_close_sites:
                    candSites.append(tmpSiteSpec.sitename)
            returnMap.setdefault(tmpDS, {})
            if sitesComDS:
                candSites = sitesComDS
            returnMap[tmpDS][cloud] = (
                candSites,
                sitesComDS,
                sitesCompPD2P,
                nUserSub,
                t1HasReplica,
                t1HasPrimary,
                nSecReplicas,
                0,
                candForMoU,
            )
        return True, returnMap

    def get_candidates(self, input_ds, prodsourcelabel, job_label, check_used_file=True, use_close_sites=False):
        """
        Get candidate sites for subscription.

        Args:
            input_ds (str): The name of the input dataset.
            prodsourcelabel (str): The label of the production source.
            job_label (str): The label of the job.
            check_used_file (bool, optional): Flag to check used file. Defaults to True.
            use_close_sites (bool, optional): Flag to use close sites. Defaults to False.

        Returns:
            tuple: A tuple containing the status (bool) and the result (dict or str).
        """
        # Get replica locations
        status, tmpRepMaps = self.get_replica_locations(input_ds, check_used_file)
        if not status:
            return status, tmpRepMaps

        # Get candidate sites
        return self.get_candidate_sites(tmpRepMaps, prodsourcelabel, job_label, use_close_sites)

    def get_list_dataset_replicas(self, dataset, max_attempts=3):
        """
        Get the list of replicas for a given dataset.

        Args:
            dataset (str): The name of the dataset.
            max_attempts (int, optional): The maximum number of attempts to get the replicas. Defaults to 3.

        Returns:
            tuple: A tuple containing the status (bool) and the result (dict or str).
        """
        for attempt in range(max_attempts):
            self.put_log(f"{attempt}/{max_attempts} listDatasetReplicas {dataset}")
            status, replicas = rucioAPI.listDatasetReplicas(dataset)
            if status == 0:
                self.put_log(f"getListDatasetReplicas->{str(replicas)}")
                return True, replicas
            time.sleep(10)

        self.put_log(f"bad response for {dataset}", "error")
        return False, {}

    def get_list_dataset_replicas_in_container(self, container, max_attempts=3):
        """
        Get the list of replicas for a given container.

        Args:
            container (str): The name of the container.
            max_attempts (int, optional): The maximum number of attempts to get the replicas. Defaults to 3.

        Returns:
            tuple: A tuple containing the status (bool) and the result (dict or str).
        """
        # response for failure
        res_for_failure = False, {}

        # get datasets in container
        for attempt in range(max_attempts):
            self.put_log(f"{attempt}/{max_attempts} listDatasetsInContainer {container}")
            datasets, out = rucioAPI.listDatasetsInContainer(container)
            if datasets is not None:
                break
            time.sleep(60)

        if datasets is None:
            self.put_log(out, "error")
            self.put_log(f"bad DDM response for {container}", "error")
            return res_for_failure

        # loop over all datasets
        all_rep_map = {}
        for dataset in datasets:
            # get replicas
            status, tmp_rep_sites = self.get_list_dataset_replicas(dataset)
            if not status:
                return res_for_failure
            # append
            all_rep_map[dataset] = tmp_rep_sites

        # return
        self.put_log("getListDatasetReplicasInContainer done")
        return True, all_rep_map

    def get_used_datasets(self, dataset_map, max_attempts=3):
        """
        Get the datasets that are used by jobs.

        Args:
            dataset_map (dict): The map of datasets.
            max_attempts (int, optional): The maximum number of attempts to get the file list. Defaults to 3.

        Returns:
            tuple: A tuple containing the status (bool) and the used datasets list.
        """
        res_for_failure = (False, [])
        used_ds_list = []

        # loop over all datasets
        for dataset_name in dataset_map:
            # get file list
            for attempt in range(max_attempts):
                try:
                    self.put_log(f"{attempt}/{max_attempts} listFilesInDataset {dataset_name}")
                    file_items, out = rucioAPI.listFilesInDataset(dataset_name)
                    status = True
                    break
                except Exception:
                    status = False
                    err_type, err_value = sys.exc_info()[:2]
                    out = f"{err_type} {err_value}"
                    time.sleep(60)

            if not status:
                self.put_log(out, "error")
                self.put_log(f"bad DDM response to get size of {dataset_name}", "error")
                return res_for_failure

            # check if jobs use the dataset
            used_flag = False
            for tmp_job in self.jobs:
                for tmp_file in tmp_job.Files:
                    if tmp_file.type == "input" and tmp_file.lfn in file_items:
                        used_flag = True
                        break
                # escape
                if used_flag:
                    break

            # used
            if used_flag:
                used_ds_list.append(dataset_name)

        # return
        self.put_log(f"used datasets = {str(used_ds_list)}")
        return True, used_ds_list

    def get_file_from_dataset(self, dataset_name, guid, max_attempts=3):
        """
        Get file information from a dataset.

        Args:
            dataset_name (str): The name of the dataset.
            guid (str): The GUID of the file.
            max_attempts (int, optional): The maximum number of attempts to get the file list. Defaults to 3.

        Returns:
            tuple: A tuple containing the status (bool) and the file information (dict or None).
        """
        res_for_failure = (False, None)

        # get files in datasets
        global g_files_in_ds_map
        if dataset_name not in g_files_in_ds_map:
            # get file list
            for attempt in range(max_attempts):
                try:
                    self.put_log(f"{attempt}/{max_attempts} listFilesInDataset {dataset_name}")
                    file_items, out = rucioAPI.listFilesInDataset(dataset_name)
                    status = True
                    break
                except Exception:
                    status = False
                    err_type, err_value = sys.exc_info()[:2]
                    out = f"{err_type} {err_value}"
                    time.sleep(60)

            if not status:
                self.put_log(out, "error")
                self.put_log(f"bad DDM response to get size of {dataset_name}", "error")
                return res_for_failure
                # append
            g_files_in_ds_map[dataset_name] = file_items

        # check if file is in the dataset
        for tmp_lfn in g_files_in_ds_map[dataset_name]:
            tmp_val = g_files_in_ds_map[dataset_name][tmp_lfn]
            if uuid.UUID(tmp_val["guid"]) == uuid.UUID(guid):
                ret_map = tmp_val
                ret_map["lfn"] = tmp_lfn
                ret_map["dataset"] = dataset_name
                return True, ret_map

        return res_for_failure

    def register_dataset_container_with_datasets(self, container_name, files, replica_map, n_sites=1, owner=None,
                                                 max_attempts=3):
        """
        Register a new dataset container with datasets.

        Args:
            container_name (str): The name of the container.
            files (list): The list of files to be included in the dataset.
            replica_map (dict): The map of replicas.
            n_sites (int, optional): The number of sites. Defaults to 1.
            owner (str, optional): The owner of the dataset. Defaults to None.
            max_attempts (int, optional): The maximum number of attempts to register the container. Defaults to 3.

        Returns:
            bool: The status of the registration process.
        """
        # parse DN
        if owner is not None:
            status, user_info = rucioAPI.finger(owner)
            if not status:
                self.put_log(f"failed to finger: {user_info}")
            else:
                owner = user_info["nickname"]
            self.put_log(f"parsed DN={owner}")

        # sort by locations
        files_map = {}
        for tmp_file in files:
            tmp_locations = sorted(replica_map[tmp_file["dataset"]])
            new_locations = []
            # skip STAGING
            for tmp_location in tmp_locations:
                if not tmp_location.endswith("STAGING"):
                    new_locations.append(tmp_location)
            if not new_locations:
                continue
            tmp_locations = new_locations
            tmp_key = tuple(tmp_locations)
            files_map.setdefault(tmp_key, [])
            # append file
            files_map[tmp_key].append(tmp_file)

        # get nfiles per dataset
        n_files_per_dataset, tmp_r = divmod(len(files), n_sites)
        if n_files_per_dataset == 0:
            n_files_per_dataset = 1
        max_files_per_dataset = 1000
        if n_files_per_dataset >= max_files_per_dataset:
            n_files_per_dataset = max_files_per_dataset

        # register new datasets
        dataset_names = []
        tmp_index = 1
        for tmp_locations in files_map:
            tmp_files = files_map[tmp_locations]
            tmp_sub_index = 0
            while tmp_sub_index < len(tmp_files):
                tmp_ds_name = container_name[:-1] + "_%04d" % tmp_index
                tmp_ret = self.register_dataset_with_location(
                    tmp_ds_name,
                    tmp_files[tmp_sub_index: tmp_sub_index + n_files_per_dataset],
                    tmp_locations,
                    owner=None,
                )
                # failed
                if not tmp_ret:
                    self.put_log(f"failed to register {tmp_ds_name}", "error")
                    return False
                # append dataset
                dataset_names.append(tmp_ds_name)
                tmp_index += 1
                tmp_sub_index += n_files_per_dataset

        # register container
        for attempt in range(max_attempts):
            try:
                self.put_log(f"{attempt}/{max_attempts} registerContainer {container_name}")
                status = rucioAPI.registerContainer(container_name, dataset_names)
                out = "OK"
                break
            except Exception:
                status = False
                err_type, err_value = sys.exc_info()[:2]
                out = f"{err_type} {err_value}"
                time.sleep(10)

        if not status:
            self.put_log(out, "error")
            self.put_log(f"bad DDM response to register {container_name}", "error")
            return False

        # return
        self.put_log(out)
        return True

    def register_dataset_with_location(self, dataset_name, files, locations, owner=None, max_attempts=3):
        """
        Register a new dataset with locations.

        Args:
            dataset_name (str): The name of the dataset.
            files (list): The list of files to be included in the dataset.
            locations (list): The list of locations where the dataset will be registered.
            owner (str, optional): The owner of the dataset. Defaults to None.
            max_attempts (int, optional): The maximum number of attempts to register the dataset. Defaults to 3.

        Returns:
            bool: The status of the registration process.
        """
        res_for_failure = False

        # get file info
        guids = []
        lfns = []
        fsizes = []
        chksums = []
        for tmp_file in files:
            guids.append(tmp_file["guid"])
            lfns.append(tmp_file["scope"] + ":" + tmp_file["lfn"])
            fsizes.append(int(tmp_file["filesize"]))
            chksums.append(tmp_file["checksum"])

        # register new dataset
        for attempt in range(max_attempts):
            try:
                self.put_log(f"{attempt}/{max_attempts} registerNewDataset {dataset_name} len={len(files)}")
                out = rucioAPI.registerDataset(dataset_name, lfns, guids, fsizes, chksums, lifetime=14)
                self.put_log(out)
                break
            except Exception:
                err_type, err_value = sys.exc_info()[:2]
                self.put_log(f"{err_type} {err_value}", "error")
                if attempt + 1 == max_attempts:
                    self.put_log(f"failed to register {dataset_name} in rucio")
                    return res_for_failure
                time.sleep(10)

        # freeze dataset
        for attempt in range(max_attempts):
            self.put_log(f"{attempt}/{max_attempts} freezeDataset {dataset_name}")
            try:
                rucioAPI.closeDataset(dataset_name)
                status = True
            except Exception:
                err_type, err_value = sys.exc_info()[:2]
                out = f"failed to freeze : {err_type} {err_value}"
                status = False
            if not status:
                time.sleep(10)
            else:
                break
        if not status:
            self.put_log(out, "error")
            self.put_log(f"bad DDM response to freeze {dataset_name}", "error")
            return res_for_failure

        # register locations
        for tmp_location in locations:
            for attempt in range(max_attempts):
                try:
                    self.put_log(f"{attempt}/{max_attempts} registerDatasetLocation {dataset_name} {tmp_location}")
                    out = rucioAPI.registerDatasetLocation(dataset_name, [tmp_location], 14, owner)
                    self.put_log(out)
                    status = True
                    break
                except Exception:
                    status = False
                    err_type, err_value = sys.exc_info()[:2]
                    self.put_log(f"{err_type} {err_value}", "error")
                    if attempt + 1 == max_attempts:
                        self.put_log(f"failed to register {dataset_name} in rucio")
                        return res_for_failure
                    time.sleep(10)
            if not status:
                self.put_log(out, "error")
                self.put_log(f"bad DDM response to set owner {dataset_name}", "error")
                return res_for_failure
        return True

    def list_datasets_by_guids(self, guids, ds_filters, max_attempts=3):
        """
        List datasets by GUIDs.

        Args:
            guids (list): The list of GUIDs.
            ds_filters (list): The list of dataset filters.
            max_attempts (int, optional): The maximum number of attempts to list the datasets. Defaults to 3.

        Returns:
            tuple: A tuple containing the status (bool) and the result (dict or str).
        """
        res_for_failure = (False, {})
        res_for_fatal = (False, {"isFatal": True})

        # get size of datasets
        for attempt in range(max_attempts):
            self.put_log(f"{attempt}/{max_attempts} listDatasetsByGUIDs GUIDs={str(guids)}")
            try:
                out = rucioAPI.listDatasetsByGUIDs(guids)
                status = True
                break
            except Exception:
                err_type, err_value = sys.exc_info()[:2]
                out = f"failed to get datasets with GUIDs : {err_type} {err_value}"
                status = False
                time.sleep(10)

        if not status:
            self.put_log(out, "error")
            self.put_log("bad response to list datasets by GUIDs", "error")
            if "DataIdentifierNotFound" in out:
                self.put_log("DataIdentifierNotFound in listDatasetsByGUIDs", "error")
                return res_for_fatal
            return res_for_failure

        self.put_log(out)

        # get map
        ret_map = {}
        try:
            out_map = out
            for guid in guids:
                tmp_ds_names = []
                # GUID not found
                if guid not in out_map:
                    self.put_log(f"GUID={guid} not found", "error")
                    return res_for_fatal

                # ignore junk datasets
                for tmp_ds_name in out_map[guid]:
                    if (
                            tmp_ds_name.startswith("panda")
                            or tmp_ds_name.startswith("user")
                            or tmp_ds_name.startswith("group")
                            or tmp_ds_name.startswith("archive")
                            or re.search("_sub\d+$", tmp_ds_name) is not None
                            or re.search("_dis\d+$", tmp_ds_name) is not None
                            or re.search("_shadow$", tmp_ds_name) is not None
                    ):
                        continue
                    # check with filters
                    if ds_filters != []:
                        flag_match = False
                        for tmp_filter in ds_filters:
                            if fnmatch.fnmatchcase(tmp_ds_name, tmp_filter):
                                flag_match = True
                                break
                        # not match
                        if not flag_match:
                            continue
                    # append
                    tmp_ds_names.append(tmp_ds_name)
                # empty
                if not tmp_ds_names:
                    self.put_log(f"no datasets found for GUID={guid}")
                    continue
                # duplicated
                if len(tmp_ds_names) != 1:
                    self.put_log(f"use the first dataset in {str(tmp_ds_names)} for GUID:{guid}")
                # append
                ret_map[guid] = tmp_ds_names[0]
        except Exception:
            self.put_log("failed to list datasets by GUIDs", "error")
            return res_for_failure
        return True, ret_map

    def convert_evt_run_to_datasets(self, run_evt_list, ds_type, stream_name, ds_filters, ami_tag, user,
                                    run_evt_guid_map, ei_api):
        """
        Convert event/run list to datasets.

        Args:
            run_evt_list (list): The list of run events.
            ds_type (str): The type of the dataset.
            stream_name (str): The name of the stream.
            ds_filters (list): The list of dataset filters.
            ami_tag (str): The AMI tag.
            user (str): The user.
            run_evt_guid_map (dict): The map of run events to GUIDs.
            ei_api (str): The EventIndex API.

        Returns:
            tuple: A tuple containing the status (bool), the result (dict or str), and the list of all files.
        """
        self.put_log(
            f"convertEvtRunToDatasets type={ds_type} stream={stream_name} dsPatt={str(ds_filters)} amitag={ami_tag}")
        # check data type
        failed_ret = False, {}, []
        fatal_ret = False, {"isFatal": True}, []
        stream_ref = "Stream" + ds_type
        # import event lookup client
        if run_evt_guid_map == {}:
            if len(run_evt_list) == 0:
                self.put_log("Empty list for run and events was provided", type="error")
                return failed_ret
            # Hadoop EI
            from .eventLookupClientEI import eventLookupClientEI

            elssi_if = eventLookupClientEI()
            # loop over all events
            n_events_per_loop = 500
            i_events_total = 0
            while i_events_total < len(run_evt_list):
                tmp_run_evt_list = run_evt_list[i_events_total: i_events_total + n_events_per_loop]
                self.put_log(f"EI lookup for {i_events_total}/{len(run_evt_list)}")
                i_events_total += n_events_per_loop
                reg_start = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                guid_list_elssi, tmp_com, tmp_out, tmp_err = elssi_if.do_lookup(
                    tmp_run_evt_list,
                    stream=stream_name,
                    tokens=stream_ref,
                    amitag=ami_tag,
                    user=user,
                    ei_api=ei_api,
                )
                reg_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - reg_start
                self.put_log(f"EI command: {tmp_com}")
                self.put_log(
                    f"took {reg_time.seconds}.{reg_time.microseconds / 1000:03f} sec for {len(tmp_run_evt_list)} events")
                # failed
                if tmp_err not in [None, ""] or len(guid_list_elssi) == 0:
                    self.put_log(tmp_com)
                    self.put_log(tmp_out)
                    self.put_log(tmp_err)
                    self.put_log("invalid return from EventIndex", type="error")
                    return failed_ret
                # check events
                for run_nr, evt_nr in tmp_run_evt_list:
                    param_str = f"Run:{run_nr} Evt:{evt_nr} Stream:{stream_name}"
                    self.put_log(param_str)
                    tmp_run_evt_key = (int(run_nr), int(evt_nr))
                    # not found
                    if tmp_run_evt_key not in guid_list_elssi or len(guid_list_elssi[tmp_run_evt_key]) == 0:
                        self.put_log(tmp_com)
                        self.put_log(tmp_out)
                        self.put_log(tmp_err)
                        err_str = f"no GUIDs were found in EventIndex for {param_str}"
                        self.put_log(err_str, type="error")
                        return fatal_ret
                    # append
                    run_evt_guid_map[tmp_run_evt_key] = guid_list_elssi[tmp_run_evt_key]
        # convert to datasets
        all_datasets = []
        all_files = []
        all_locations = {}
        for tmp_idx in run_evt_guid_map:
            tmp_guids = run_evt_guid_map[tmp_idx]
            run_nr, evt_nr = tmp_idx
            tmp_ds_ret, tmp_ds_map = self.list_datasets_by_guids(tmp_guids, ds_filters)
            # failed
            if not tmp_ds_ret:
                self.put_log("failed to convert GUIDs to datasets", type="error")
                if "isFatal" in tmp_ds_map and tmp_ds_map["isFatal"] is True:
                    return fatal_ret
                return failed_ret
            # empty
            if tmp_ds_map == {}:
                self.put_log(f"there is no dataset for Run:{run_nr} Evt:{evt_nr} GUIDs:{str(tmp_guids)}", type="error")
                return fatal_ret
            if len(tmp_ds_map) != 1:
                self.put_log(
                    f"there are multiple datasets {str(tmp_ds_map)} for Run:{run_nr} Evt:{evt_nr} GUIDs:{str(tmp_guids)}",
                    type="error")
                return fatal_ret

            # append
            for tmp_guid in tmp_ds_map:
                tmp_ds_name = tmp_ds_map[tmp_guid]
                # collect dataset names
                if tmp_ds_name not in all_datasets:
                    all_datasets.append(tmp_ds_name)
                    # get location
                    stat_rep, replica_map = self.get_list_dataset_replicas(tmp_ds_name)
                    # failed
                    if not stat_rep:
                        self.put_log(f"failed to get locations for {tmp_ds_name}", type="error")
                        return failed_ret
                    # collect locations
                    tmp_location_list = []
                    for tmp_location in replica_map:
                        # use only complete replicas
                        ds_stat_dict = replica_map[tmp_location][0]
                        if ds_stat_dict["total"] is not None and ds_stat_dict["total"] == ds_stat_dict["found"]:
                            if tmp_location not in tmp_location_list:
                                tmp_location_list.append(tmp_location)
                    all_locations[tmp_ds_name] = tmp_location_list

                # get file info
                tmp_file_ret, tmp_file_info = self.get_file_from_dataset(tmp_ds_name, tmp_guid)
                # failed
                if not tmp_file_ret:
                    self.put_log(f"failed to get fileinfo for GUID:{tmp_guid} DS:{tmp_ds_name}", type="error")
                    return failed_ret
                # collect files
                all_files.append(tmp_file_info)
        # return
        self.put_log(f"converted to {str(all_datasets)}, {str(all_locations)}, {str(all_files)}")
        return True, all_locations, all_files

    def put_log(self, message, message_type="debug", send_log=False, action_tag="", tags_map={}):
        """
        Log a message with a specific type and optionally send it to a logger.

        Args:
            message (str): The message to be logged.
            message_type (str, optional): The type of the message. Defaults to "debug".
            send_log (bool, optional): Flag to send the message to a logger. Defaults to False.
            action_tag (str, optional): The action tag. Defaults to "".
            tags_map (dict, optional): The map of tags. Defaults to {}.

        """
        if self.logger is None:
            temp_message = self.token + " " + str(message)
        else:
            temp_message = str(message)

        if message_type == "error":
            if self.logger is None:
                _logger.error(temp_message)
            else:
                self.logger.error(temp_message)
            # keep last error message
            self.last_message = temp_message
        else:
            if self.logger is None:
                _logger.debug(temp_message)
            else:
                self.logger.debug(temp_message)

        # send to logger
        if send_log:
            temp_message = self.token + " - "
            if action_tag != "":
                temp_message += f"action={action_tag} "
                for tmp_tag in tags_map:
                    tmp_tag_val = tags_map[tmp_tag]
                    temp_message += f"{tmp_tag}={tmp_tag_val} "
            temp_message += "- " + message
            temp_panda_logger = PandaLogger()
            temp_panda_logger.lock()
            temp_panda_logger.setParams({"Type": "pd2p"})
            temp_log = temp_panda_logger.getHttpLogger(panda_config.loggername)
            # add message
            if message_type == "error":
                temp_log.error(temp_message)
            else:
                temp_log.info(temp_message)
            # release HTTP handler
            temp_panda_logger.release()
            time.sleep(1)