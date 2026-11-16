package com.margosmark.fintracker.http

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Sealing for at-rest cookie material (docs/52 §2 rec 2).
 *
 * [encrypt]/[decrypt] take and return raw bytes so the store stays agnostic
 * of the algorithm; the runtime implementation is [KeystoreGcmCipher] and
 * unit tests use an in-memory test implementation (the AndroidKeyStore
 * provider does not exist on a JVM test classpath).
 */
interface PayloadCipher {
    /** @return ciphertext with whatever framing the implementation needs (IV, magic). */
    fun encrypt(plaintext: ByteArray): ByteArray

    fun decrypt(ciphertext: ByteArray): ByteArray

    /**
     * Destroy the backing key material (docs/54 §4.2 L3): a purge must delete
     * the KEY, not only the ciphertext — a surviving key leaves a recoverable
     * session on disk. Idempotent; safe to call when no key exists yet.
     */
    fun destroy()
}

/**
 * The selected persistence protection (docs/52 §2 rec 2 asked for a selection):
 * AES-256-GCM with a key that lives in the Android Keystore.
 *
 * The guarantee, stated exactly: the key backing this cipher is generated
 * inside the Keystore (hardware-backed — StrongBox/TEE — where the device
 * offers it), is NON-EXPORTABLE, and can only be used by this app's UID.
 * A copied cookie-store file is therefore ciphertext that no other app, and
 * no offline attacker without the device's own keystore, can decrypt. This is
 * protection-at-rest for a bearer credential — NOT device-binding and NOT
 * protection against a compromised running process (docs/52 is explicit that
 * HttpOnly does not transfer to native, and this class does not claim more).
 *
 * No third-party library is used (docs/53 non-goal: no SDKs without a privacy
 * decision) — plain platform APIs only, so the dependency surface is OkHttp
 * alone.
 *
 * Failure mode: any decrypt/parse failure at load time makes the store delete
 * its file and start signed-out (fail closed — an unreadable credential is
 * treated as no credential, never bypassed).
 */
class KeystoreGcmCipher(
    private val alias: String = "fintracker_cookie_store_v1",
) : PayloadCipher {

    override fun encrypt(plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val iv = cipher.iv
        return iv + cipher.doFinal(plaintext)
    }

    override fun decrypt(ciphertext: ByteArray): ByteArray {
        require(ciphertext.size > IV_BYTES) { "ciphertext too short" }
        val iv = ciphertext.copyOfRange(0, IV_BYTES)
        val body = ciphertext.copyOfRange(IV_BYTES, ciphertext.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(GCM_TAG_BITS, iv))
        return cipher.doFinal(body)
    }

    /** docs/54 §4.2 L3 — delete the Keystore entry itself (idempotent). */
    override fun destroy() {
        synchronized(this) {
            val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
            if (keyStore.containsAlias(alias)) keyStore.deleteEntry(alias)
        }
    }

    /** Synchronized: two racing first-run inits must not each mint a key (the loser's key would orphan the file). */
    private fun key(): SecretKey = synchronized(this) {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getEntry(alias, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        generator.generateKey()
    }

    companion object {
        private const val KEYSTORE = "AndroidKeyStore"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
        private const val GCM_TAG_BITS = 128
    }
}
