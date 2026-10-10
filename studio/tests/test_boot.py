"""Run: uv run python -m tests.test_boot

The start-up sequence on a scripted command runner: no Docker is touched. A missing Docker or
a container that never turns healthy fails with words a person can act on, and stops there.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from studio.boot import Boot
from studio.settings import Settings

DESK = Path(__file__).parent / 'fixtures'
SETTINGS = replace(Settings.from_env(), root=DESK, agents='*/agent.yaml')


def scripted(answers: dict[str, tuple[int, str]]):
    """A runner answering by the command's last word; it records every command it was given."""
    seen: list[list[str]] = []

    async def runner(argv: Sequence[str], env: dict[str, str]) -> tuple[int, str]:
        seen.append(list(argv))
        assert env['STUDIO_PG_PORT'] == str(SETTINGS.pg_port)
        return answers.get(argv[-1], (0, ''))

    return runner, seen


def statuses(boot: Boot) -> dict[str, str]:
    return {step.id: step.status for step in boot.state().steps}


def a_healthy_start_reads_the_agents():
    runner, seen = scripted({'--short': (0, '2.29.7')})
    boot = Boot(SETTINGS, runner=runner)
    assert not boot.state().ready and statuses(boot)['docker'] == 'waiting'
    state = asyncio.run(boot.start())
    assert statuses(boot) == {
        'docker': 'healthy', 'postgres': 'healthy', 'redis': 'healthy', 'engine': 'healthy', 'agents': 'healthy',
        'web': statuses(boot)['web'],
    }  # fmt: skip
    assert state.ready == (statuses(boot)['web'] in ('healthy', 'skipped')) and not state.failed
    assert state.agents == ['desk']
    up = seen[1]
    assert up[-5:] == ['up', '--detach', '--wait', 'postgres', 'redis'] and SETTINGS.project in up, up


def no_docker_stops_with_what_to_install():
    runner, seen = scripted({'--short': (127, 'docker: not found')})
    boot = Boot(SETTINGS, runner=runner)
    state = asyncio.run(boot.start())
    assert state.failed and not state.ready and len(seen) == 1
    docker = next(step for step in state.steps if step.id == 'docker')
    assert 'compose v2' in docker.message, docker.message
    assert statuses(boot)['agents'] == 'waiting', 'nothing after a failed step runs'


def a_container_that_never_turns_healthy_says_why():
    runner, _ = scripted({'redis': (1, 'pulling\ncontainer slng-studio-postgres-1 is unhealthy')})
    state = asyncio.run(Boot(SETTINGS, runner=runner).start())
    postgres = next(step for step in state.steps if step.id == 'postgres')
    assert state.failed and postgres.status == 'failed' and 'unhealthy' in postgres.message, postgres


def no_services_leaves_docker_alone():
    runner, seen = scripted({})
    boot = Boot(SETTINGS, services=False, runner=runner)
    asyncio.run(boot.start())
    assert seen == [] and statuses(boot)['postgres'] == 'skipped' and statuses(boot)['agents'] == 'healthy'


if __name__ == '__main__':
    a_healthy_start_reads_the_agents()
    no_docker_stops_with_what_to_install()
    a_container_that_never_turns_healthy_says_why()
    no_services_leaves_docker_alone()
    print('boot ok: healthy start, no docker, unhealthy container, no services')
