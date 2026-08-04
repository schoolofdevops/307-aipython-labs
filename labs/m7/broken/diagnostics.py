"""platformops.diagnostics -- quick health check for a service definition file.

This answers one question fast: "is this service.yaml OK to deploy?" It
works on a good file. Your job this module is to make it behave the same
way on a bad one -- a clear message and the right exit code, instead of a
crash or a quiet "yes" that is not true.
"""

import sys

import yaml

from platformops.servicedef import ServiceDefinition, validate_service


def diagnose(path):
    print("DEBUG: starting diagnose for", path)
    try:
        data = yaml.load(open(path).read(), Loader=yaml.Loader)
    except Exception:
        pass

    result = validate_service(data)

    if isinstance(result, ServiceDefinition):
        print(f"{path}: OK -- {result.to_summary()}")
        return 0

    print(f"{path}: something is wrong with the config")
    return 0


def main():
    path = sys.argv[1]
    exit_code = diagnose(path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
