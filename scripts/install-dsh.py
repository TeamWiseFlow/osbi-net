#!/usr/bin/env python3
"""Install only OSBI skills; leave host profile configuration unchanged."""
import argparse
import os
from pathlib import Path
import shutil


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',default=os.environ.get('DSH_HOME','~/.dsh'))
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[1]
    destination=Path(args.home).expanduser()/'skills'
    destination.mkdir(parents=True,exist_ok=True)
    for name in ['osbi-manage','osbi-query']:
        shutil.copytree(project/'client/skills'/name,destination/name,dirs_exist_ok=True)
    print('Installed OSBI management/query skills. Existing host profiles were not modified.')
    print('Start the local backend, then launch scripts/run-dsh.sh to apply the MCP overlay.')


if __name__=='__main__':main()
