// Which Claude Code session the app shows. Every window with the plugin
// pushes its own snapshots; the one running the voice helper here (isOwner)
// wins, else the one heard from last. A session that missed three heartbeats
// is gone. Staleness is judged by when the app received a push, never by its
// `at`, which comes from another process's clock and only orders one
// session's snapshots.

import type { AppSnapshot } from '../shared/snapshot'

/** A session is live this long after its last accepted push (three missed 2 s heartbeats). */
export const STALE_MS = 6000
/** Sessions remembered at most; the board is a display, not a log. */
export const MAX_SESSIONS = 16

type Entry = { snapshot: AppSnapshot; receivedAt: number }

export class SessionBoard {
  private readonly entries = new Map<string, Entry>()

  /** Stores a pushed snapshot; false when it is older than one already held for its session. */
  accept(snapshot: AppSnapshot, receivedAt: number): boolean {
    const held = this.entries.get(snapshot.sessionId)
    if (held !== undefined && held.snapshot.at > snapshot.at) return false
    this.entries.set(snapshot.sessionId, { snapshot, receivedAt })
    for (const [id, entry] of this.entries) {
      if (receivedAt - entry.receivedAt >= STALE_MS) this.entries.delete(id)
    }
    while (this.entries.size > MAX_SESSIONS) {
      const oldest = this.oldest()
      if (oldest === undefined) break
      this.entries.delete(oldest)
    }
    return true
  }

  /** The snapshot to show at `now`: the newest live owner, else the newest live session. */
  current(now: number): AppSnapshot | undefined {
    let owner: Entry | undefined
    let latest: Entry | undefined
    for (const entry of this.entries.values()) {
      if (now - entry.receivedAt >= STALE_MS) continue
      if (latest === undefined || entry.receivedAt >= latest.receivedAt) latest = entry
      if (entry.snapshot.isOwner && (owner === undefined || entry.receivedAt >= owner.receivedAt)) owner = entry
    }
    return (owner ?? latest)?.snapshot
  }

  liveCount(now: number): number {
    let count = 0
    for (const entry of this.entries.values()) if (now - entry.receivedAt < STALE_MS) count += 1
    return count
  }

  private oldest(): string | undefined {
    let oldest: [string, Entry] | undefined
    for (const pair of this.entries) if (oldest === undefined || pair[1].receivedAt < oldest[1].receivedAt) oldest = pair
    return oldest?.[0]
  }
}
