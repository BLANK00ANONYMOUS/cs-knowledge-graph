---
title: System Calls & the User/Kernel Boundary
type: concept
domain: systems
tags: []
prereqs:
- processes-and-threads
- c-programming
related: []
sources: []
mastery: 0
last_reviewed: null
---
# System Calls & the User/Kernel Boundary

> syscall instruction, privilege rings, libc wrappers, strace, errno.

## Why it matters

Sandboxes (seccomp), EDR hooks and kernel exploits all live at this boundary.

## Builds on

- [[processes-and-threads|Processes & Threads]]
- [[c-programming|C Programming]]

## Key ideas

_Fill in as you learn — in your own words._

## Check yourself

1. Trace `cat file` with strace — which syscalls appear?
2. Why is a syscall more expensive than a function call?

## My notes

