#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright © 2022 Intel Corporation

"""Tool that automatically applies patches and tests them as possible."""

from __future__ import annotations
import asyncio
import sys
import typing

from pick import core

import aiohttp


async def revert() -> None:
    p = await asyncio.create_subprocess_exec(
        'git', 'reset', '--hard', 'HEAD~',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()


async def reset() -> None:
    p = await asyncio.create_subprocess_exec(
        'git', 'reset', '--hard', 'HEAD',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()


async def git_push(commit: typing.Optional[core.Commit], commits: typing.List[core.Commit],
                   force: bool = False) -> None:
    cmd = ['git', 'push']
    if force:
        cmd.append('-f')
    p = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await p.wait() != 0:
        print('  Critical Error: failed to push to gitlib')
        await revert()
        if commit is not None:
            commit.resolution = core.Resolution.MANUAL_RESOLUTION
            core.save(commits)
        sys.exit(1)


async def main(loop: asyncio.BaseEventLoop) -> None:
    commits = await core.update_commits()
    new_commits = [c for c in commits if
                   c.nominated and c.resolution is core.Resolution.UNRESOLVED]
    failed: typing.Set[str] = set()

    with open('VERSION', 'r') as f:
        version = f.read().split('-')[0].strip()
    version = '.'.join(version.split('.')[:2])
    url = 'https://gitlab.freedesktop.org/api/v4/projects/176/pipelines'
    params = {
        'ref': f'staging/{version}',
        'per_page': '1',
    }

    lock = asyncio.Lock()

    for commit in reversed(new_commits):
        async with lock:
            print(f'Commit: {commit.sha}: {commit.description}')
            if commit.because_sha in failed:
                # This isn't actually failed, but in a case like:
                # C requires B, B requires A, A fails to apply
                # We want C to be excluded as well
                failed.add(commit.sha)
                print('  Not applying because the commit it fixes was not applied successfully')
                continue
            result, _ = await commit.apply()
            if not result:
                failed.add(commit.sha)
                print(f'  FAILED to apply: {commit.sha}: {commit.description}')
                await reset()
                commit.resolution = core.Resolution.MANUAL_RESOLUTION
                continue

            print('  Compiling project')
            # TODO: make builddir configureable?
            p = await asyncio.create_subprocess_exec(
                'ninja', '-C', 'builddir', 'test',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await p.wait() != 0:
                failed.add(commit.sha)
                print(f'  FAILED to compile: {commit.sha}: {commit.description}, reverting')
                await revert()
                commit.resolution = core.Resolution.MANUAL_RESOLUTION
                continue

            print('  Pushing update to git')
            await git_push(commit, commits)

            print('  Waiting for for CI to finish: ', end='', flush=True)
            async with aiohttp.ClientSession(loop=loop) as session:
                async with session.get(url, params=params) as response:
                    content = await response.json()
                id_ = content[0]['id']
                while True:
                    async with session.get(f'{url}/{id_}') as response:
                        content = await response.json()
                    status: str = content['status']
                    if status in {'created', 'waiting_for_resources', 'preparing', 'pending',
                                  'running', 'scheduled'}:
                        print('.', end='', flush=True)
                        await asyncio.sleep(30)
                        continue
                    elif status == 'success':
                        print(f'\n  Successfully applied: {commit.sha}')
                        commit.resolution = core.Resolution.MERGED
                        break
                    else:
                        if status == 'failed':
                            print(f'\n  CI Failed: {commit.sha}')
                        else:
                            print(f'\n  Unexpected CI status "{status}": {commit.sha}')
                        failed.add(commit.sha)
                        await revert()
                        commit.resolution = core.Resolution.MANUAL_RESOLUTION
                        # delete the bad commit from upstream
                        await git_push(commit, commits, True)
                        break

    core.save(commits)

    # Update the pick_log, and push it
    await core.commit_state()
    await git_push(None, commits)
    sys.exit(0)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(loop))
