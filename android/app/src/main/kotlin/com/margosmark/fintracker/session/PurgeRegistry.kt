package com.margosmark.fintracker.session

/**
 * The registry of every account-derived local store the app owns (query
 * caches, widgets, snapshots — the UI card registers its stores here).
 *
 * Purging is the tenant-isolation primitive (docs/52 §2 rec 3, docs/53 AC3.1):
 * on logout, session expiry and identity change, EVERY registered store is
 * purged SYNCHRONOUSLY before a different identity can render.
 *
 * Failure semantics are deliberately harsh: every store is given its chance
 * (one broken store cannot silently skip the rest), and any failure aborts
 * the commit with [PurgeFailedException] — the session manager refuses to
 * present the new identity rather than risk showing stale tenant data
 * (fail closed; same arbitration principle as docs/01 for financial data).
 */
class PurgeRegistry {

    fun interface PurgeableStore {
        fun purge()
    }

    private val lock = Any()
    private val stores = LinkedHashMap<String, PurgeableStore>()

    fun register(name: String, store: PurgeableStore) {
        require(name.isNotBlank()) { "store name required" }
        synchronized(lock) {
            require(!stores.containsKey(name)) { "store already registered: $name" }
            stores[name] = store
        }
    }

    fun registeredNames(): List<String> = synchronized(lock) { stores.keys.toList() }

    /**
     * Run every purge in registration order, synchronously, on the caller's
     * thread. Throws [PurgeFailedException] naming the failures if any store
     * failed — callers MUST NOT continue to an identity transition on that.
     */
    fun purgeAll() {
        val snapshot: List<Pair<String, PurgeableStore>> = synchronized(lock) { stores.entries.map { it.key to it.value } }
        val failed = mutableListOf<String>()
        for ((name, store) in snapshot) {
            try {
                store.purge()
            } catch (_: Exception) {
                failed += name
            }
        }
        if (failed.isNotEmpty()) {
            throw PurgeFailedException(failed)
        }
    }
}

class PurgeFailedException(val failedStores: List<String>) :
    RuntimeException("purge failed for stores: ${failedStores.joinToString(", ")} — refusing identity transition")
