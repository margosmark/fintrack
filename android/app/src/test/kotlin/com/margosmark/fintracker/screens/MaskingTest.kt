package com.margosmark.fintracker.screens

import com.margosmark.fintracker.FlushesBeforeWaitRule

import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.margosmark.fintracker.ui.masking.MaskAuthEffects
import com.margosmark.fintracker.ui.masking.MaskController
import com.margosmark.fintracker.ui.masking.MaskLifecycleObserver
import com.margosmark.fintracker.ui.masking.MaskReason
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/**
 * AC2: masking is session-scoped and boots ON (restore-off by construction —
 * there is no persistence to restore from), and every re-mask trigger fires
 * before the app-switcher could snapshot the values.
 */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34])
class MaskingTest {

    @get:Rule
    val rule = FlushesBeforeWaitRule(createComposeRule())

    @Test
    fun bootsMaskedNoMatterWhat() {
        assertTrue(MaskController(initiallyMasked = true).masked)
    }

    @Test
    fun backgroundOrDeviceLockRemasksBeforeAnySnapshot() {
        val mask = MaskController(initiallyMasked = true)
        mask.reveal()
        assertFalse(mask.masked)

        val owner = object : LifecycleOwner {
            val registry = LifecycleRegistry(this)
            override val lifecycle: Lifecycle get() = registry
        }
        owner.registry.handleLifecycleEvent(Lifecycle.Event.ON_CREATE)
        owner.registry.handleLifecycleEvent(Lifecycle.Event.ON_START)
        owner.registry.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
        owner.lifecycle.addObserver(MaskLifecycleObserver(mask))

        owner.registry.handleLifecycleEvent(Lifecycle.Event.ON_PAUSE)
        owner.registry.handleLifecycleEvent(Lifecycle.Event.ON_STOP)
        assertTrue("stopped screens must be masked", mask.masked)
    }

    @Test
    fun explicitLogoutRemasks() {
        val mask = MaskController(initiallyMasked = true)
        mask.mask(MaskReason.LOGOUT)
        assertTrue(mask.masked)
    }

    @Test
    fun sessionTransitionsDriveRemask() {
        val mask = MaskController(initiallyMasked = true)
        val userId = mutableStateOf<Int?>(1)
        val explicitLogout = mutableStateOf(false)
        rule.setContent {
            MaskAuthEffects(mask, authenticatedUserId = userId.value, explicitLogout = explicitLogout.value)
        }
        rule.waitForIdle()

        mask.reveal()
        rule.waitForIdle()
        assertFalse(mask.masked)

        // A silent same-identity session refresh keeps the user's reveal (AC2.2).
        userId.value = 1
        rule.waitForIdle()
        assertFalse("same user must not lose the reveal", mask.masked)

        // Session expiry: the authenticated id becomes null.
        userId.value = null
        rule.waitForIdle()
        assertTrue("expiry must re-mask", mask.masked)

        // Identity change: a DIFFERENT id replaces the old one.
        userId.value = 2
        rule.waitForIdle()
        mask.reveal()
        rule.waitForIdle()
        assertFalse(mask.masked)
        userId.value = 3
        rule.waitForIdle()
        assertTrue("identity change must re-mask", mask.masked)

        // Explicit logout is named as such (distinct reason), and masks.
        explicitLogout.value = true
        userId.value = null
        rule.waitForIdle()
        assertTrue("logout must re-mask", mask.masked)
    }
}
