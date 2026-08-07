#!/usr/bin/env python3
"""Make the upstream MLP MNSIM participant runnable inside the image.

LEGOSim's original source embeds two paths from its development workstation.
This patch replaces only those paths with the packaged MNSIMChiplet directory;
the PipeComm message sequence and the Python MNSIM computation are unchanged.
"""
from pathlib import Path


SOURCE = Path("/opt/legosim/artifact/MLP/mnsim.cpp")
OLD_COMMAND = (
    'system("cd /home/qc/test_simulator/Chiplet_Heterogeneous_newVersion/'
    'MNSIMChiplet; pip install torch torchvision ; python3 MNSIM_Chiplet.py '
    '-ID1 0 -ID2 2");'
)
NEW_COMMAND = (
    'system("cd /opt/legosim/MNSIMChiplet && python3 MNSIM_Chiplet.py '
    '-ID1 0 -ID2 2");'
)
OLD_RESULT = (
    'std::ifstream inputFile("/home/qc/Chiplet_Heterogeneous_newVersion_gem5/'
    'Chiplet_Heterogeneous_newVersion/MNSIMChiplet/result_0_2.res");'
)
NEW_RESULT = 'std::ifstream inputFile("/opt/legosim/MNSIMChiplet/result_0_2.res");'
PYTHON_SOURCE = Path("/opt/legosim/MNSIMChiplet/MNSIM_Chiplet.py")
OLD_CONFIG_PATH = (
    "/home/qc/Chiplet_Heterogeneous_newVersion_gem5/"
    "Chiplet_Heterogeneous_newVersion/tasks-vgg13/"
)
NEW_CONFIG_PATH = "/opt/legosim/tasks-vgg13/"
OLD_MAIN_COMMAND = "cd MNSIM; python ./main.py -NN "
NEW_MAIN_COMMAND = "cd MNSIM; /usr/bin/python3 ./main.py -NN "
OLD_ID_ARGUMENTS = " + ' -Id1 ' + str(id[0]) + ' -Id2 ' + str(id[1])"
COMPAT_BACKEND_MARKER = "mnsim_compat.py"


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    # Some preserved server base images already replace the unpublished MNSIM
    # fork with the checked-in source-calibrated compatibility backend.  That
    # is a valid, self-contained state once Dockerfile copies mnsim_compat.py;
    # never try to apply the older workstation-path rewrite a second time.
    if COMPAT_BACKEND_MARKER in source:
        return
    if NEW_COMMAND not in source or NEW_RESULT not in source:
        if OLD_COMMAND not in source or OLD_RESULT not in source:
            raise RuntimeError(f"unexpected upstream MNSIM layout: {SOURCE}")
        source = source.replace(OLD_COMMAND, NEW_COMMAND, 1)
        source = source.replace(OLD_RESULT, NEW_RESULT, 1)
        SOURCE.write_text(source, encoding="utf-8")
    elif OLD_COMMAND in source or OLD_RESULT in source:
        raise RuntimeError(f"unexpected upstream MNSIM layout: {SOURCE}")

    python_source = PYTHON_SOURCE.read_text(encoding="utf-8")
    if OLD_CONFIG_PATH in python_source:
        PYTHON_SOURCE.write_text(
            python_source.replace(OLD_CONFIG_PATH, NEW_CONFIG_PATH), encoding="utf-8"
        )
    elif NEW_CONFIG_PATH not in python_source:
        raise RuntimeError(f"unexpected MNSIM Python layout: {PYTHON_SOURCE}")

    python_source = PYTHON_SOURCE.read_text(encoding="utf-8")
    if OLD_MAIN_COMMAND in python_source:
        PYTHON_SOURCE.write_text(
            python_source.replace(OLD_MAIN_COMMAND, NEW_MAIN_COMMAND, 1),
            encoding="utf-8",
        )
    elif NEW_MAIN_COMMAND not in python_source:
        raise RuntimeError(f"unexpected MNSIM command layout: {PYTHON_SOURCE}")

    python_source = PYTHON_SOURCE.read_text(encoding="utf-8")
    if OLD_ID_ARGUMENTS in python_source:
        PYTHON_SOURCE.write_text(
            python_source.replace(OLD_ID_ARGUMENTS, "", 1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
