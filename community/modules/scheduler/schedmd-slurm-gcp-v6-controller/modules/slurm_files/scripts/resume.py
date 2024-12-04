#!/usr/bin/env python3

# Copyright (C) SchedMD LLC.
# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional, Dict, Collection
import argparse
from datetime import timedelta
import shlex
import json
import logging
import os
import yaml
from itertools import chain
from pathlib import Path
from dataclasses import dataclass

import util
from util import (
    chunked,
    ensure_execute,
    execute_with_futures,
    get_insert_operations,
    log_api_request,
    map_with_futures,
    run,
    separate,
    to_hostlist_fast,
    trim_self_link,
    wait_for_operation,
)
from util import lookup, NSDict

import slurm_gcp_plugins

log = logging.getLogger()

PLACEMENT_MAX_CNT = 150
# Placement group needs to be the same for an entire bulk_insert hence
# if placement is used the actual BULK_INSERT_LIMIT will be
# max([1000, PLACEMENT_MAX_CNT])
BULK_INSERT_LIMIT = 5000


@dataclass(frozen=True)
class ResumeJobData:
    job_id: int
    partition: str
    nodes_alloc: List[str]

@dataclass(frozen=True)
class ResumeData:
    jobs: List[ResumeJobData]

def get_resume_file_data() -> Optional[ResumeData]:
    if not (path := os.getenv("SLURM_RESUME_FILE")):
        log.error("SLURM_RESUME_FILE was not in environment. Cannot get detailed job, node, partition allocation data.")
        return None
    blob = Path(path).read_text()
    log.debug(f"Resume data: {blob}")
    data = json.loads(blob)

    jobs = []
    for jo in data.get("jobs", []):

        job = ResumeJobData(
            job_id = jo.get("job_id"),
            partition = jo.get("partition"),
            nodes_alloc = util.to_hostnames(jo.get("nodes_alloc")),
        )
        jobs.append(job)
    return ResumeData(jobs=jobs)

def instance_properties(nodeset:object, model:str, placement_group:Optional[str], labels:Optional[dict], job_id:Optional[int]):
    props = NSDict()

    if labels: # merge in extra labels on instance and disks
        template = lookup().node_template(model)
        template_info = lookup().template_info(template)

        props.labels = {**template_info.labels, **labels}
        
        for disk in template_info.disks:
            if disk.initializeParams.get("diskType", "local-ssd") == "local-ssd":
                continue # do not label local ssd
            disk.initializeParams.labels.update(labels)
        props.disks = template_info.disks

    if placement_group:
        props.scheduling.onHostMaintenance = "TERMINATE"
        props.resourcePolicies = [placement_group]

    if reservation := lookup().nodeset_reservation(nodeset):
        props.reservationAffinity = {
            "consumeReservationType": "SPECIFIC_RESERVATION",
            "key": f"compute.{util.universe_domain()}/reservation-name",
            "values": [reservation.bulk_insert_name],
        }

        if reservation.deployment_type == "DENSE":
            props.scheduling.provisioning_model = "RESERVATION_BOUND"

        if reservation.policies:
            props.scheduling.onHostMaintenance = "TERMINATE"
            props.resourcePolicies = reservation.policies
            log.info(
                f"reservation {reservation.bulk_insert_name} is being used with policies {props.resourcePolicies}"
            )
        else:
            props.resourcePolicies = []
            log.info(
                f"reservation {reservation.bulk_insert_name} is being used without any policies"
            )

    if nodeset.maintenance_interval:
        props.scheduling.maintenanceInterval = nodeset.maintenance_interval

    if nodeset.dws_flex.enabled:
        update_props_dws(props, nodeset.dws_flex, job_id)

    # Override with properties explicit specified in the nodeset
    props.update(nodeset.get("instance_properties") or {})
    
    return props

def update_props_dws(props:object, dws_flex:object, job_id: Optional[int]) -> None:
    props.scheduling.onHostMaintenance = "TERMINATE"
    props.scheduling.instanceTerminationAction = "DELETE"
    props.reservationAffinity['consumeReservationType'] = "NO_RESERVATION"
    props.scheduling.maxRunDuration['seconds'] = dws_flex_duration(dws_flex, job_id)

def dws_flex_duration(dws_flex:object, job_id: Optional[int]) -> int:
    max_duration = dws_flex.max_run_duration
    if dws_flex.use_job_duration and job_id is not None and (job := lookup().job(job_id)) and job.duration:
        if timedelta(seconds=30) <= job.duration <= timedelta(weeks=2):
            max_duration = int(job.duration.total_seconds())
        else:
            log.info("Job TimeLimit cannot be less than 30 seconds or exceed 2 weeks")
    return max_duration


def per_instance_properties(node):
    props = NSDict()
    # No properties beyond name are supported yet.

    return props

def create_instances_request(nodes, partition_name, placement_group, job_id=None):
    """Call regionInstances.bulkInsert to create instances"""
    assert 0 < len(nodes) <= BULK_INSERT_LIMIT

    # model here indicates any node that can be used to describe the rest
    model = next(iter(nodes))
    nodeset = lookup().node_nodeset(model)
    template = lookup().node_template(model)
    partition = lookup().cfg.partitions[partition_name]
    log.debug(f"create_instances_request: {model} placement: {placement_group}")

    body = NSDict()

    body.count = len(nodes)

    if placement_group:
        assert len(nodes) <= PLACEMENT_MAX_CNT
        pass # do not set minCount to force "all or nothing" behavior
    else:
        body.minCount = 1

    # source of instance properties
    body.sourceInstanceTemplate = template

    labels = (
        dict(slurm_job_id=job_id)
        if job_id is not None and partition.enable_job_exclusive
        else None
    )
    # overwrites properties across all instances
    body.instanceProperties = instance_properties(
        nodeset, model, placement_group, labels, job_id
    )

    # key is instance name, value overwrites properties
    body.perInstanceProperties = {k: per_instance_properties(k) for k in nodes}

    zone_allow = nodeset.zone_policy_allow or []
    zone_deny = nodeset.zone_policy_deny or []

    if len(zone_allow) == 1: # if only one zone is used, use zonal BulkInsert API, as less prone to errors
        api_method = lookup().compute.instances().bulkInsert
        method_args = {"zone": zone_allow[0]}
    else:
        api_method = lookup().compute.regionInstances().bulkInsert
        method_args = {"region": lookup().node_region(model)}
        
        body.locationPolicy.locations = {
            **{ f"zones/{z}": {"preference": "ALLOW"} for z in zone_allow },
            **{ f"zones/{z}": {"preference": "DENY"} for z in zone_deny }}
        body.locationPolicy.targetShape = nodeset.zone_target_shape
    
    if lookup().cfg.enable_slurm_gcp_plugins:
        slurm_gcp_plugins.pre_instance_bulk_insert(
            lkp=lookup(),
            nodes=nodes,
            placement_group=placement_group,
            request_body=body,
        )

    req = api_method(
        project=lookup().project, 
        body=body.to_dict(), 
        **method_args)
    log.debug(f"new request: endpoint={req.methodId} nodes={to_hostlist_fast(nodes)}")
    log_api_request(req)
    return req


@dataclass(frozen=True)
class BulkChunk:
    nodes: List[str]
    prefix: str
    chunk_idx: int
    job_id: Optional[int]
    partition: Optional[str]
    placement_group: Optional[str] = None
    

def group_nodes_bulk(nodes: List[str], resume_data: Optional[ResumeData], lkp: util.Lookup):
    """group nodes by job_id, placement_group, node_group, and max bulkInsert size"""
    if resume_data is None: # all nodes will be considered jobless
        resume_data = ResumeData(jobs=[])
        
    nodes = set(nodes) # turn into set to simplify intersection

    @dataclass(frozen=True)
    class JobGroup: # aux struct
        job_id: Optional[int]
        partition: Optional[str]
        placement_groups: Dict[str, List[str]]

    job_groups = {}

    # expand all job nodelists
    for job in resume_data.jobs:
        nodes_resume = nodes & set(job.nodes_alloc)
        if lkp.partition_is_tpu(job.partition): # don't create placement groups for TPU
            pgs = {None: sorted(nodes_resume)}
        else:
            # create placement groups if nodes for job need it
            pgs = create_placement_groups(job.nodes_alloc, job.job_id)

            # placement group assignment is based on all allocated nodes, but we only want to
            # handle nodes in nodes_resume in this run.
            for pg, pg_nodes in pgs.items():
                pgs[pg] = sorted(set(pg_nodes) & nodes_resume)
        
        job_groups[job.job_id] = JobGroup(
            job_id=job.job_id,
            partition=job.partition,
            placement_groups=pgs,
        )

    all_jobless_nodes = nodes.difference(
            chain.from_iterable(j.nodes_alloc for j in resume_data.jobs))
    jobless_nodes, jobless_nodes_tpu = util.separate(lkp.node_is_tpu, all_jobless_nodes)
    
    job_groups["Normal_None"] = JobGroup(
        job_id=None,
        placement_groups=create_placement_groups(sorted(jobless_nodes), job_id=0),
        partition=None,
    )
    job_groups["TPU_None"] = JobGroup(
        job_id=None,
        placement_groups={None: sorted(jobless_nodes_tpu)},
        partition=None,
    )

    def chunk_nodes(nodes: List[str]):
        chunk_size = BULK_INSERT_LIMIT
        if nodes and lkp.node_is_tpu(nodes[0]):
            chunk_size = util.TPU(lkp.node_nodeset(nodes[0])).vmcount
        return chunked(nodes, n=chunk_size)

    grouped_nodes = [
        BulkChunk(
            nodes=nodes_chunk,
            prefix=prefix,
            job_id = job.job_id,
            partition = job.partition,
            placement_group=placement_group,
            chunk_idx=i)

        for job in job_groups.values()
        for placement_group, pg_nodes in job.placement_groups.items()
        for prefix, nodes in util.groupby_unsorted(pg_nodes, lkp.node_prefix)
        for i, nodes_chunk in enumerate(chunk_nodes(list(nodes)))
    ]
    
    def group_name(chunk: BulkChunk):
        if chunk.placement_group is not None:
            return f"{chunk.prefix}:job{chunk.job_id}:{chunk.placement_group}:{chunk.chunk_idx}"
        if chunk.job_id is not None:
            return f"{chunk.prefix}:job{chunk.job_id}:{chunk.chunk_idx}"
        return f"{chunk.prefix}:{chunk.chunk_idx}"

    return {group_name(chunk): chunk for chunk in grouped_nodes}


def start_tpu(data):
    tpu = data["tpu"]
    node = data["node"]
    if len(node) == 1:
        node = node[0]
        log.debug(
            f"Will create a TPU of type {tpu.node_type} tf_version {tpu.tf_version} in zone {tpu.zone} with name {node}"
        )
        tpunode = tpu.get_node(node)
        if tpunode is None:
            if not tpu.create_node(nodename=node):
                log.error("Error creating tpu node {node}")
        else:
            if tpu.preserve_tpu:
                if not tpu.start_node(nodename=node):
                    log.error("Error starting tpu node {node}")
            else:
                log.info(
                    f"Tpu node {node} is already created, but will not start it because nodeset does not have preserve_tpu option active."
                )
    else:
        log.debug(
            f"Will create a multi-vm TPU of type {tpu.node_type} tf_version {tpu.tf_version} in zone {tpu.zone} with name {node[0]}"
        )
        if not tpu.create_node(nodename=node):
            log.error("Error creating tpu node {node}")


def resume_nodes(nodes: List[str], resume_data: Optional[ResumeData]):
    """resume nodes in nodelist"""
    if not nodes:
        log.info("No nodes to resume")
        return

    nodes = sorted(nodes, key=lookup().node_prefix)
    grouped_nodes = group_nodes_bulk(nodes, resume_data, lookup())

    if log.isEnabledFor(logging.DEBUG):
        grouped_nodelists = {
            group: to_hostlist_fast(chunk.nodes) for group, chunk in grouped_nodes.items()
        }
        log.debug(
            "node bulk groups: \n{}".format(yaml.safe_dump(grouped_nodelists).rstrip())
        )

    tpu_start_data = []
    tpu_objs = {}
    bi_inserts = {}

    for group, chunk in grouped_nodes.items():
        if chunk.partition and lookup().partition_is_tpu(chunk.partition):
            # do not create multiple tpu_objs if nodes with the same prefix are used
            if chunk.prefix not in tpu_objs.keys():
                model = chunk.nodes[0]
                tpu_objs[chunk.prefix] = util.TPU(lookup().node_nodeset(model))
            tpu_start_data.append({"tpu": tpu_objs[chunk.prefix], "node": chunk.nodes})
        else:
            bi_inserts[group] = create_instances_request(
                chunk.nodes, chunk.partition, chunk.placement_group, chunk.job_id
            )

    # execute all bulkInsert requests  with batch
    bulk_ops = dict(
        zip(bi_inserts.keys(), map_with_futures(ensure_execute, bi_inserts.values()))
    )
    log.debug(f"bulk_ops={yaml.safe_dump(bulk_ops)}")
    started = {
        group: op for group, op in bulk_ops.items() if not isinstance(op, Exception)
    }
    failed = {
        group: err for group, err in bulk_ops.items() if isinstance(err, Exception)
    }
    if failed:
        failed_reqs = [str(e) for e in failed.items()]
        log.error("bulkInsert API failures: {}".format("; ".join(failed_reqs)))
        for ident, exc in failed.items():
            down_nodes_notify_jobs(grouped_nodes[ident].nodes, f"GCP Error: {exc._get_reason()}", resume_data)

    if log.isEnabledFor(logging.DEBUG):
        for group, op in started.items():
            group_nodes = grouped_nodelists[group]
            name = op["name"]
            gid = op["operationGroupId"]
            log.debug(
                f"new bulkInsert operation started: group={group} nodes={group_nodes} name={name} operationGroupId={gid}"
            )
    # wait for all bulkInserts to complete and log any errors
    bulk_operations = {group: wait_for_operation(op) for group, op in started.items()}

    # Start TPU after regular nodes so that regular nodes are not affected by the slower TPU nodes
    log.debug(f"tpu_start_data={yaml.safe_dump(tpu_start_data)}")
    execute_with_futures(start_tpu, tpu_start_data)

    all_successful_inserts = []

    for group, bulk_op in bulk_operations.items():
        group_id = bulk_op["operationGroupId"]
        bulk_op_name = bulk_op["name"]
        if "error" in bulk_op:
            error = bulk_op["error"]["errors"][0]
            group_nodes = to_hostlist_fast(grouped_nodes[group].nodes)
            log.warning(
                f"bulkInsert operation errors: {error['code']} name={bulk_op_name} operationGroupId={group_id} nodes={group_nodes}"
            )
        successful_inserts, failed_inserts = separate(
            lambda op: "error" in op, get_insert_operations(group_id)
        )
        # Apparently multiple errors are possible... so join with +.
        by_error_inserts = util.groupby_unsorted(
            failed_inserts,
            lambda op: "+".join(err["code"] for err in op["error"]["errors"]),
        )
        for code, failed_ops in by_error_inserts:
            failed_nodes = {trim_self_link(op["targetLink"]): op for op in failed_ops}
            hostlist = util.to_hostlist(failed_nodes)
            count = len(failed_nodes)
            log.error(
                f"{count} instances failed to start: {code} ({hostlist}) operationGroupId={group_id}"
            )
            failed_node, failed_op = next(iter(failed_nodes.items()))
            msg = "; ".join(
                f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
                for err in failed_op["error"]["errors"]
            )
            if code != "RESOURCE_ALREADY_EXISTS":
                down_nodes_notify_jobs(failed_nodes, f"GCP Error: {msg}", resume_data)
            log.error(
                f"errors from insert for node '{failed_node}' ({failed_op['name']}): {msg}"
            )

        ready_nodes = {trim_self_link(op["targetLink"]) for op in successful_inserts}
        if len(ready_nodes) > 0:
            ready_nodelist = to_hostlist_fast(ready_nodes)
            log.info(f"created {len(ready_nodes)} instances: nodes={ready_nodelist}")
            all_successful_inserts.extend(successful_inserts)


def down_nodes_notify_jobs(nodes: List[str], reason: str, resume_data: Optional[ResumeData]) -> None:
    """set nodes down with reason"""
    nodelist = util.to_hostlist_fast(nodes)
    reason_quoted = shlex.quote(reason)
    
    log.error(f"Marking nodes {nodelist} as DOWN, reason: {reason}")
    run(f"{lookup().scontrol} update nodename={nodelist} state=down reason={reason_quoted}")

    if resume_data is None:
        log.warning("Cannot update and notify jobs with API failures as no valid resume file is present.")
        return
    
    nodes = set(nodes) # turn into set to speed up intersection
    for job in resume_data.jobs:
        if not (set(job.nodes_alloc) & nodes):
            continue
        run(f"{lookup().scontrol} update jobid={job.job_id} admincomment='{reason_quoted}'")
        run(f"{lookup().scontrol} notify {job.job_id} '{reason_quoted}'")


def hold_job(job_id, reason):
    """hold job, set comment to reason"""
    run(f"{lookup().scontrol} hold jobid={job_id}")
    run(f"{lookup().scontrol} update jobid={job_id} comment='{reason}'")


def create_placement_request(pg_name, region):
    config = {
        "name": pg_name,
        "region": region,
        "groupPlacementPolicy": {
            "collocation": "COLLOCATED",
        },
    }
    if lookup().cfg.enable_slurm_gcp_plugins:
        slurm_gcp_plugins.pre_placement_group_insert(
            lkp=lookup(), pg_name=pg_name, region=region, request_body=config
        )
    request = lookup().compute.resourcePolicies().insert(
        project=lookup().project, region=region, body=config
    )
    log_api_request(request)
    return request


def create_placement_groups(node_list: List[str], job_id:int) -> Dict[str, List[str]]:
    pgs = {}
    node_map = lookup().nodeset_map(node_list)
    for _, nodes in node_map.items():
        pgs.update(create_nodeset_placement_groups(nodes, job_id))
    return pgs


def create_nodeset_placement_groups(node_list: List[str], job_id:int) -> Dict[str, List[str]]:
    no_pg = {None: node_list} # canned result for no placement policies created

    if len(node_list) < 2:
        return no_pg # don't create placement_policy for just one node
    
    model = next(iter(node_list))
    nodeset = lookup().node_nodeset(model)
    if not (nodeset.enable_placement and valid_placement_nodes(node_list)):
        return no_pg
    
    region = lookup().node_region(model)

    groups = {
        f"{lookup().cfg.slurm_cluster_name}-slurmgcp-managed-{nodeset.nodeset_name}-{job_id}-{i}": nodes
        for i, nodes in enumerate(chunked(node_list, n=PLACEMENT_MAX_CNT))
    }

    if log.isEnabledFor(logging.DEBUG):
        debug_groups = {
            group: to_hostlist_fast(nodes) for group, nodes in groups.items()
        }
        log.debug(
            f"creating {len(groups)} placement groups: \n{yaml.safe_dump(debug_groups).rstrip()}"
        )
    requests = {
        group: create_placement_request(group, region) for group in groups.keys()
    }
    ops = dict(
        zip(requests.keys(), map_with_futures(ensure_execute, requests.values()))
    )

    def classify_result(item):
        op = item[1]
        if not isinstance(op, Exception):
            return "submitted"
        if all(e.get("reason") == "alreadyExists" for e in op.error_details):
            return "redundant"
        return "failed"

    grouped_ops = dict(util.groupby_unsorted(list(ops.items()), classify_result))
    submitted, redundant, failed = (
        dict(grouped_ops.get(key, {})) for key in ("submitted", "redundant", "failed")
    )
    if redundant:
        log.warning(
            "placement policies already exist: {}".format(",".join(redundant.keys()))
        )
    if failed:
        reqs = [f"{e}" for _, e in failed.values()]
        log.fatal("failed to create placement policies: {}".format("; ".join(reqs)))
    operations = {group: wait_for_operation(op) for group, op in submitted.items()}
    for group, op in operations.items():
        if "error" in op:
            msg = "; ".join(
                f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
                for err in op["error"]["errors"]
            )
            log.error(
                f"placement group failed to create: '{group}' ({op['name']}): {msg}"
            )

    log.info(
        f"created {len(operations)} placement groups ({to_hostlist_fast(operations.keys())})"
    )
    return groups


def valid_placement_nodes(nodelist):
    invalid_types = frozenset(["e2", "t2d", "n1", "t2a", "m1", "m2", "m3"])
    for node in nodelist:
        mt = lookup().node_template_info(node).machineType
        if mt.split("-")[0] in invalid_types:
            log.warn(f"Unsupported machine type for placement policy: {mt}.")
            log.warn(
                f"Please do not use any the following machine types with placement policy: ({','.join(invalid_types)})"
            )
            return False
    return True


def main(nodelist: str) -> None:
    """main called when run as script"""
    log.debug(f"ResumeProgram {nodelist}")
    # Filter out nodes not in config.yaml
    other_nodes, nodes = separate(
        lookup().is_power_managed_node, util.to_hostnames(nodelist)
    )
    if other_nodes:
        log.error(
            f"Ignoring non-power-managed nodes '{to_hostlist_fast(other_nodes)}' from '{nodelist}'"
        )

    if not nodes:
        log.info("No nodes to resume")
        return

    resume_data = get_resume_file_data()
    log.info(f"resume {util.to_hostlist_fast(nodes)}")
    resume_nodes(nodes, resume_data)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("nodelist", help="list of nodes to resume")
    args = util.init_log_and_parse(parser)
    main(args.nodelist)
