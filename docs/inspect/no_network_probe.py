"""End-to-end probe for Inspect AI on agent's rootless Docker: no model key, no network.

The solver runs one command inside the sandbox container (compose.yaml: network_mode none) and
the task passes only if that command could not open a connection. Run it from an unsandboxed
agent shell, see docs/rootless-docker.md:

    inspect eval docs/inspect/no_network_probe.py --model mockllm/model
"""
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import sandbox

PROBE = """
import socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    print("REACHED")
except OSError as error:
    print("NO-NETWORK", error)
"""


@solver
def run_probe():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        result = await sandbox().exec(["python3", "-c", PROBE], timeout=30)
        state.output.completion = result.stdout.strip()
        return state

    return solve


@task
def no_network_probe():
    return Task(
        dataset=[Sample(input="Report whether this sandbox has network access.", target="NO-NETWORK")],
        solver=run_probe(),
        scorer=includes(),
        sandbox=("docker", "compose.yaml"),
    )
