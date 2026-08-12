#!/bin/sh
nsenter -t 1 -n ss -tan state established | tail -n +2 | wc -l