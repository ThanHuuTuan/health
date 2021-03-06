#!/usr/bin/python

# Copyright 2016: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import json
import logging

import requests

from health.drivers import driver
from health.drivers import utils

LOG = logging.getLogger(__name__)


class Driver(driver.Base):

    STATS = {
        "http_response_time_stats": {
            "extended_stats": {
                "field": "http_response_time"
            }
        },
        "http_response_time_percentiles": {
            "percentiles": {
                "field": "http_response_time"
            }
        },
        "http_response_size_stats": {
            "extended_stats": {
                "field": "http_response_size"
            }
        },
        "http_response_size_percentiles": {
            "percentiles": {
                "field": "http_response_size"
            }
        }
    }

    AGG_REQUEST = {
        "size": 0,  # this is a count request
        "query": {
            "bool": {
                "filter": [
                    {"exists": {"field": "http_method"}},
                    {"exists": {"field": "http_status"}},
                    {"exists": {"field": "http_response_time"}},
                ]
            }
        },
        "aggs": {
            "per_minute": {
                "date_histogram": {
                    "field": "Timestamp",
                    "interval": "minute",
                    "format": "yyyy-MM-dd'T'HH:mm:ss",
                    "min_doc_count": 0
                },
                "aggs": {
                    "http_codes": {
                        "terms": {
                            "field": "http_status"
                        }
                    },
                    "services": {
                        "terms": {
                            "field": "Logger"
                        },
                        "aggs": {
                            "http_codes": {
                                "terms": {
                                    "field": "http_status"
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    AGG_REQUEST["aggs"]["per_minute"]["aggs"].update(STATS)
    AGG_REQUEST["aggs"]["per_minute"]["aggs"]["services"]["aggs"].update(STATS)

    use_keyword = None

    def get_request(self, ts_range):
        query = copy.deepcopy(self.AGG_REQUEST)

        if self.use_keyword is None:
            es = self.config["elastic_src"]
            resp = requests.get(es.rstrip("/").rsplit("/", 1)[0])
            # if there was a 4xx/5xx response: raise it
            resp.raise_for_status()

            mappings = resp.json()
            mappings = mappings[list(mappings.keys())[0]]["mappings"]
            props = mappings[list(mappings.keys())[0]]["properties"]
            self.use_keyword = "keyword" in props["Logger"].get("fields", {})

        if self.use_keyword:
            base = query["aggs"]["per_minute"]["aggs"]
            base["http_codes"]["terms"]["field"] += ".keyword"
            base["services"]["terms"]["field"] += ".keyword"
            base["services"]["aggs"]["http_codes"]["terms"][
                "field"] += ".keyword"

        query["query"]["bool"]["filter"].append({
            "range": {"Timestamp": ts_range}
        })
        return query

    def transform_http_codes(self, buckets):
        result = {"1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}

        for b in buckets:
            result["%sxx" % (int(b["key"]) // 100)] = b["doc_count"]
        return result

    def fci(self, http_codes):
        all_codes = sum(v for k, v in http_codes.items())
        if all_codes:
            return float((all_codes - http_codes["5xx"])) / all_codes
        else:
            return 1.0  # TODO(boris-42): Ignore this points.

    def record_from_bucket(self, bucket, timestamp, service):
        http_codes = self.transform_http_codes(bucket["http_codes"]["buckets"])
        record = {
            "timestamp": timestamp,
            "requests_count": bucket["doc_count"],
            "service": service,
            "fci": self.fci(http_codes),
            "http_codes": http_codes,
            "response_time": bucket["http_response_time_stats"],
            "response_size": bucket["http_response_size_stats"]
        }

        del record["response_time"]["sum_of_squares"]
        del record["response_size"]["sum_of_squares"]

        for el in ["response_time", "response_size"]:
            for pth in ["50.0", "95.0", "99.0"]:
                value = bucket["http_%s_percentiles" % el]["values"][pth]
                record[el]["%sth" % pth[:-2]] = value

        return record

    def fetch(self, latest_aggregated_ts=None):
        es = self.config["elastic_src"]
        ts_min, ts_max = utils.get_min_max_timestamps(es, "Timestamp")

        if ts_min is ts_max is None:
            LOG.error("Got no timestamps from source es, will skip fetching "
                      "data for %s", es)
            return

        if latest_aggregated_ts:
            intervals = utils.incremental_scan(ts_max, latest_aggregated_ts)
        else:
            intervals = utils.incremental_scan(ts_max, ts_min)

        for interval in intervals:
            body = self.get_request(interval)
            try:
                resp = requests.post("%s/_search" % es,
                                     data=json.dumps(body))
            except requests.exceptions.RequestException as e:
                LOG.error("Was unable to make a request for interval %s: %s",
                          interval, e)

            if not resp.ok:
                LOG.error("Got a non-ok response for interval %s: %s",
                          interval, resp.text)
                continue
            resp = resp.json()

            r = []
            for bucket in resp["aggregations"]["per_minute"]["buckets"]:

                ts = bucket["key_as_string"]
                r.append(self.record_from_bucket(bucket, ts, "all"))

                for service in bucket["services"]["buckets"]:
                    r.append(
                        self.record_from_bucket(service, ts, service["key"]))
            yield r
