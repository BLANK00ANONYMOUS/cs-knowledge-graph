---
title: Memory Allocators (malloc internals)
type: concept
domain: systems
tags: []
prereqs:
- virtual-memory
- pointers-and-memory-layout
related: []
sources: []
mastery: 0
last_reviewed: null
---
# Memory Allocators (malloc internals)

> How malloc/free work: bins, chunks, free lists, arenas; glibc ptmalloc, jemalloc, slab allocators.

## Why it matters

Heap exploitation and performance tuning both require allocator knowledge.

## Builds on

- [[virtual-memory|Virtual Memory & Paging]]
- [[pointers-and-memory-layout|Pointers & Process Memory Layout]]

## Key ideas

_Fill in as you learn — in your own words._

## Check yourself

1. Implement a simple bump allocator and a free-list allocator.
2. What metadata does a glibc chunk header contain?

## My notes

