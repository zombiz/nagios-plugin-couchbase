#!/usr/bin/env python

"""
Collects statistics from the Couchbase REST API.
See: https://developer.couchbase.com/documentation/server/current/rest-api/rest-intro.html

#### Dependencies

 * python-requests
 * PyYAML

For Nagios:
 * nsca-client or nsca-ng-client
"""

import argparse
import json
import logging as log
import logging.config
import numbers
import operator
import os
import requests
import subprocess
import sys
import yaml


# Basic setup
parser = argparse.ArgumentParser(usage="%(prog)s [options] -c CONFIG_FILE")
parser.add_argument("-a", "--all-nodes", dest="all_nodes", action="store_true", help="Return metrics for all nodes")
parser.add_argument("-c", "--config", required=True, dest="config_file", action="store", help="Path to the check_couchbase YAML file")
parser.add_argument("-d", "--dump-services",  dest="dump_services", action="store_true", help="Print service descriptions and exit")
parser.add_argument("-n", "--no-metrics",  dest="no_metrics", action="store_true", help="Do not send metrics to the monitoring host")
parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Enable debug logging to console")
parser.add_argument("-C", "--couchbase-host",  dest="couchbase_host", action="store", help="Override the configured Couchbase host")
parser.add_argument("-H", "--monitor-host",  dest="monitor_host", action="store", help="Override the configured monitoring host")
parser.add_argument("-M", "--monitor-type", dest="monitor_type", action="store", help="Override the configured monitoring system type")
args = parser.parse_args()


# Attempts to load the configuration file and apply argument overrides
def load_config():
    config = []

    try:
        f = open(args.config_file).read()
        config = yaml.load(f)
    except IOError:
        print("Unable to read config file {0}".format(args.config_file))
        sys.exit(2)
    except (yaml.reader.ReaderError, yaml.parser.ParserError):
        print("Invalid YAML syntax in config file {0}".format(args.config_file))
        sys.exit(2)
    except:
        raise

    if args.all_nodes:
        config["all_nodes"] = True

    if args.dump_services:
        config["dump_services"] = True

    if args.no_metrics:
        config["send_metrics"] = False

    if args.couchbase_host:
        config["couchbase_host"] = args.couchbase_host

    if args.monitor_host:
        config["monitor_host"] = args.monitor_host

    if args.monitor_type:
        conifg["monitor_type"] - args.monitor_type

    if args.verbose:
        config["logging"]["handlers"]["console"]["level"] = "DEBUG"

    logging.config.dictConfig(config["logging"])

    config = validate_config(config)

    return config


# Validates all config except metrics
def validate_config(config):
    # set defaults
    config.setdefault("couchbase_host", "localhost")
    config.setdefault("couchbase_admin_port", 18091)
    config.setdefault("couchbase_query_port", 18093)
    config.setdefault("couchbase_ssl", True)
    config.setdefault("nsca_path", "/sbin/send_nsca")
    config.setdefault("service_include_cluster_name", False)
    config.setdefault("service_include_label", False)
    config.setdefault("send_metrics", True)
    config.setdefault("dump_services", False)
    config.setdefault("all_nodes", False)

    # Unrecoverable errors
    for item in ["couchbase_user", "couchbase_password", "monitor_type", "monitor_host", "monitor_port", "node", "data"]:
        if item not in config:
            print("{0} is not set in {1}".format(item, args.config_file))
            sys.exit(2)

    for item in config["data"]:
        if "bucket" not in item or item["bucket"] is None:
            print("Bucket name is not set in {0}".format(args.config_file))
            sys.exit(2)

        if "metrics" not in item or item["metrics"] is None:
            print("Metrics are not set for bucket {0} in {1}".format(item["bucket"], args.config_file))
            sys.exit(2)

    return config


# Validates metric config
def validate_metric(metric, samples):
    if "metric" not in metric or metric["metric"] is None:
        log.warning("Skipped: metric name not set")
        return False

    name = metric["metric"]

    if name not in samples:
        log.warning("Skipped: metric does not exist: {0}".format(name))
        return False

    if "description" not in metric or metric["description"] is None:
        log.warning("Skipped: service description is not set for metric: {0}".format(name))
        return False

    if metric["op"] not in [">", ">=", "=", "<=", "<"]:
        log.warning("Skipped: Invalid operator: {0}, for metric: {1}".format(metric["op"], name))
        return False


# Adds the ANSI bold escape sequence
def bold(string):
    return "\033[1m{0}\033[0m".format(string)


# Formats numbers with a max precision 2 and removes trailing zeros
def pretty_number(f):
    value = str(round(f, 2)).rstrip("0").rstrip(".")

    if "." in value:
        return float(value)
    elif value == "":
        return 0
    else:
        return int(value)


# Averages multiple metric samples to smooth out values
def avg(samples):
    return sum(samples, 0) / len(samples)


# For dynamic comparisons
# Thanks to https://stackoverflow.com/a/18591880
def compare(inp, relate, cut):
    ops = {">": operator.gt,
           "<": operator.lt,
           ">=": operator.ge,
           "<=": operator.le,
           "=": operator.eq}
    return ops[relate](inp, cut)


# Builds the service description based on config
# Format will be {service_prefix} {cluster_name} {label} - {description}
def build_service_description(description, cluster_name, label, config):
    service = ""

    if "service_prefix" in config:
        service += config["service_prefix"]

    if config["service_include_cluster_name"] and cluster_name:
        service += " {0}".format(cluster_name)

    if config["service_include_label"]:
        service += " {0}".format(label)

    if service != "":
        service += " - "

    service += description

    return service


# Determines metric status based on value and thresholds
def eval_status(value, critical, warning, op):
    if isinstance(critical, numbers.Number) and compare(value, op, critical):
        return 2, "CRITICAL"
    elif isinstance(critical, str) and compare(value, op, critical):
        return 2, "CRITICAL"
    elif isinstance(warning, numbers.Number) and compare(value, op, warning):
        return 1, "WARNING"
    elif isinstance(warning, str) and compare(value, op, warning):
        return 1, "WARNING"
    else:
        return 0, "OK"


# Evalutes data service stats and sends check results
def process_data_stats(host, bucket, metrics, config, results):
    s = couchbase_request(host, "/pools/default/buckets/{0}/stats".format(bucket), config)
    stats = s["op"]["samples"]

    for m in metrics:
        m.setdefault("crit", None)
        m.setdefault("warn", None)
        m.setdefault("op", ">=")

        if m["metric"] == "percent_quota_utilization":
            value = avg(stats["mem_used"]) / (avg(stats["ep_mem_high_wat"]) * 1.0) * 100
        elif m["metric"] == "percent_metadata_utilization":
            value = avg(stats["ep_meta_data_memory"]) / (avg(stats["ep_mem_high_wat"]) * 1.0) * 100
        elif m["metric"] == "disk_write_queue":
            value = avg(stats["ep_queue_size"]) + avg(stats["ep_flusher_todo"])
        elif m["metric"] == "total_ops":
            value = 0
            for op in ["cmd_get", "cmd_set", "incr_misses", "incr_hits", "decr_misses", "decr_hits", "delete_misses", "delete_hits"]:
                value += avg(stats[op])
        else:
            if validate_metric(m, stats) is False:
                continue

            value = avg(stats[m["metric"]])

        results.append({"host": host, "metric": m, "value": value, "label": bucket})

    return results


# Evaluates XDCR stats and sends check results
def process_xdcr_stats(host, tasks, config, results):
    for task in tasks:
        if task["type"] == "xdcr":
            if "xdcr" not in config:
                log.warning("XDCR is running but no metrics are configured")
                return

            metrics = config["xdcr"]

            for m in metrics:
                m.setdefault("crit", None)
                m.setdefault("warn", None)
                m.setdefault("op", ">=")

                # task["id"] looks like this: {GUID}/{source_bucket}/{destination_bucket}
                label = "xdcr {0}/{1}".format(task["id"].split("/")[1], task["id"].split("/")[2])

                if m["metric"] == "status":
                    value = task["status"]
                    results.append({"host": host, "metric": m, "value": value, "label": label})
                elif task["status"] in ["running", "paused"]:
                    # REST API requires the destination endpoint to be URL encoded.
                    destination = requests.utils.quote("replications/{0}/{1}".format(task["id"], m["metric"]), safe="")

                    uri = "/pools/default/buckets/{0}/stats/{1}".format(task["source"], destination)
                    stats = couchbase_request(host, uri, config)

                    for node in stats["nodeStats"]:
                        # node is formatted as host:port
                        if host == node.split(":")[0]:
                            if len(stats["nodeStats"][node]) == 0:
                                log.error("Invalid XDCR metric: {0}".format(m["metric"]))
                                return

                            value = avg(stats["nodeStats"][node])
                            results.append({"host": host, "metric": m, "value": value, "label": label})

    return results


# Evaluates query service stats and sends check results
def process_query_stats(host, config, results):
    if "query" not in config:
        log.warning("Query service is running but no metrics are configured")
        return

    metrics = config["query"]
    stats = couchbase_request(host, "/admin/stats", config, "query")

    for m in metrics:
        m.setdefault("crit", None)
        m.setdefault("warn", None)
        m.setdefault("op", ">=")

        if validate_metric(m, stats) is False:
            continue

        value = stats[m["metric"]]

        # Convert nanoseconds to milliseconds
        if m["metric"] in ["request_timer.75%", "request_timer.95%", "request_timer.99%"]:
            value = value / 1000 / 1000

        results.append({"host": host, "metric": m, "value": value, "label": "query"})

    return results


# Evaluates node stats and sends check results
def process_node_stats(host, stats, config, results):
    metrics = config["node"]

    for m in metrics:
        m.setdefault("crit", None)
        m.setdefault("warn", None)
        m.setdefault("op", "=")

        if validate_metric(m, stats) is False:
            continue

        value = str(stats[m["metric"]])

        results.append({"host": host, "metric": m, "value": value, "label": "node"})

    return results


# Executes a Couchbase REST API request and returns the output
def couchbase_request(host, uri, config, service=None):
    if service == "query":
        port = config["couchbase_query_port"]
    else:
        port = config["couchbase_admin_port"]

    if config["couchbase_ssl"]:
        protocol = "https"
    else:
        protocol = "http"

    url = "{0}://{1}:{2}{3}".format(protocol, host, str(port), uri)

    try:
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
        f = requests.get(url, auth=(config["couchbase_user"], config["couchbase_password"]), verify=False)

        status = f.status_code

        if f.text:
            response = json.loads(f.text)

        # We can provide a helpful error message on 403
        if status == 403:
            if "permissions" in response:
                print("{0}: {1}".format(response["message"], response["permissions"]))

        # Bail if status is anything but successful
        if status != 200:
            f.raise_for_status()

        return response
    except requests.exceptions.HTTPError as e:
        print("Failed to complete request to Couchbase: {0}, {1}".format(url, e))
        sys.exit(2)
    except:
        raise


# Sends a passive check result to Nagios
def send_nagios(results, cluster_name, config):
    for result in results:
        host = result["host"]
        metric = result["metric"]
        value = result["value"]
        label = result["label"]

        if isinstance(value, numbers.Number):
            value = pretty_number(value)

        service = build_service_description(metric["description"], cluster_name, label, config)
        status, status_text = eval_status(value, metric["crit"], metric["warn"], metric["op"])
        message = "{0} - {1}: {2}".format(status_text, metric["metric"], value)
        if config["dump_services"]:
            print(service)
            continue

        line = "{0}\t{1}\t{2}\t{3}\n".format(host, service, status, message)
        log.debug("{0} {1} {2} {3} {4} {5} {6} {7}".format(bold("Host:"), host, bold("Service:"), service, bold("Status:"), status, bold("Message:"), message))

        if config["send_metrics"] is False:
            continue

        if not os.path.exists(config["nsca_path"]):
            print("Path to send_nsca is invalid: {0}".format(config["nsca_path"]))
            sys.exit(2)

        cmd = "{0} -H {1} -p {2}".format(config["nsca_path"], str(config["monitor_host"]), str(config["monitor_port"]))

        try:
            pipe = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = pipe.communicate(line.encode())
            pipe.stdin.close()
            pipe.wait()

            if pipe.returncode:
                print("Failed to send metrics. {0}".format(err.decode().rstrip()))
                sys.exit(2)
        except:
            raise

    print("OK - check_couchbase ran successfully")


def main():
    config = load_config()

    results = []

    tasks = couchbase_request(config["couchbase_host"], "/pools/default/tasks", config)
    pools_default = couchbase_request(config["couchbase_host"], "/pools/default", config)

    # clusterName is optional
    if "clusterName" in pools_default:
        cluster_name = pools_default["clusterName"]
    else:
        cluster_name = None

    nodes = pools_default["nodes"]
    for node in nodes:
        if config["all_nodes"] is False and "thisNode" not in node:
            continue

        # node is formatted a hostname:port
        host = node["hostname"].split(":")[0]
        services = node["services"]

        results = process_node_stats(host, node, config, results)

        if "kv" in services:
            results = process_xdcr_stats(host, tasks, config, results)

            for item in config["data"]:
                # _all is a special case where we process stats for all buckets
                if item["bucket"] == "_all":
                    for bucket in couchbase_request(host, "/pools/default/buckets?skipMap=true", config):
                        results = process_data_stats(host, bucket["name"], item["metrics"], config, results)
                else:
                    results = process_data_stats(host, item["bucket"], item["metrics"], tasks, config, results)

        if "n1ql" in services:
            process_query_stats(host, config, results)

    if config["monitor_type"] == "nagios":
        send_nagios(results, cluster_name, config)
    else:
        print("Unknown monitor_type configured.  No metrics have been sent.")
        sys.exit(2)


if __name__ == "__main__":
    main()
