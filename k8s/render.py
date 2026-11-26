"""Render a deployment patch locally. Does not contact Kubernetes or read credentials."""
import argparse
from pathlib import Path
import re
import sys

import yaml


def render(owner_id, image):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', owner_id) or owner_id.startswith('REPLACE_'):
        raise ValueError('Provide the owner\'s immutable ZITADEL user ID')
    if not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}', image):
        raise ValueError('Provide an image reference pinned to its sha256 digest')
    patch = yaml.safe_load(Path(__file__).with_name('api-patch.yaml').read_text())
    container = patch['spec']['template']['spec']['containers'][0]
    container['image'] = image
    for item in container['env']:
        if item['name'] == 'OWNER_SUBJECT':
            item['value'] = owner_id
        elif item['name'] == 'WORKER_IMAGE':
            item['value'] = image
    return yaml.safe_dump(patch, sort_keys=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-id', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    try:
        sys.stdout.write(render(args.owner_id, args.image))
    except ValueError as error:
        parser.error(str(error))
