/**
 * WhatsApp ↔ bridgebot relay (Baileys-based)
 *
 * What it does:
 *   - Connects to WhatsApp using Baileys (prints QR to terminal on first run)
 *   - Incoming WA messages → POST to bridgebot at BRIDGEBOT_WEBHOOK_URL
 *   - Outbound: exposes HTTP endpoints so bridgebot can send replies back
 *
 * Endpoints:
 *   GET  /status         — connection health
 *   POST /send           — { jid, message }  send text
 *   POST /send-image     — { jid, path, caption? }  send image file
 *   POST /send-audio     — { jid, path }  send voice note (ptt)
 *
 * Env vars (set in ~/.jefe/secrets/.env.whatsapp-bridge):
 *   WA_BRIDGE_PORT         default 3001
 *   BRIDGEBOT_WEBHOOK_URL  default http://127.0.0.1:8591/webhook/whatsapp
 *   WA_AUTH_DIR            default ~/.jefe/wa-auth
 *   WA_BRIDGE_SECRET       shared secret sent as X-WA-Bridge-Secret header
 */

import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} from '@whiskeysockets/baileys'
import express from 'express'
import pino from 'pino'
import qrcodeTerminal from 'qrcode-terminal'
import QRCode from 'qrcode'
import { mkdirSync, writeFileSync } from 'fs'
import { writeFile } from 'fs/promises'
import { join } from 'path'
import { tmpdir, homedir } from 'os'

// ── Config ──────────────────────────────────────────────────────────────────
const BRIDGE_PORT        = parseInt(process.env.WA_BRIDGE_PORT || '3001')
const BRIDGEBOT_URL      = process.env.BRIDGEBOT_WEBHOOK_URL || 'http://127.0.0.1:8591/webhook/whatsapp'
const AUTH_DIR           = process.env.WA_AUTH_DIR || join(homedir(), '.jefe', 'wa-auth')
const BRIDGE_SECRET      = process.env.WA_BRIDGE_SECRET || ''
// Digits only, e.g. "16466750765" — if set, uses phone pairing instead of QR
const PHONE_NUMBER       = (process.env.WA_PHONE_NUMBER || '').replace(/\D/g, '')

mkdirSync(AUTH_DIR, { recursive: true })

// ── Logger ───────────────────────────────────────────────────────────────────
const logger = pino({ level: 'info', transport: { target: 'pino/file', options: { destination: 1 } } })

// ── State ────────────────────────────────────────────────────────────────────
let sock = null
let isConnected = false
let reconnectAttempts = 0
const MAX_RECONNECTS = 20

// ── Baileys connection ───────────────────────────────────────────────────────
async function connectToWA() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version } = await fetchLatestBaileysVersion()
  console.log(`[bridge] Using WA version: ${version.join('.')}`)

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,   // we handle QR ourselves for nicer output
    logger: pino({ level: 'silent' }),
    browser: ['Bridgebot', 'Chrome', '120.0'],
    markOnlineOnConnect: false,
    syncFullHistory: false,
    connectTimeoutMs: 30000,
    defaultQueryTimeoutMs: 30000,
  })

  sock.ev.on('creds.update', saveCreds)

  // Phone number pairing: request code as soon as socket is ready (not registered yet)
  if (PHONE_NUMBER && !state.creds.registered) {
    // Give the socket a moment to initialize before requesting
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(PHONE_NUMBER)
        const formatted = code.match(/.{1,4}/g)?.join('-') || code
        console.log(`\n[bridge] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`)
        console.log(`[bridge] Phone pairing code: ${formatted}`)
        console.log(`[bridge] Enter this in WhatsApp → Linked Devices → Link with phone number`)
        console.log(`[bridge] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`)
        // Save code to file so the setup wizard can display it live
        writeFileSync(join(AUTH_DIR, 'pairing_code.txt'), formatted)
        writeFileSync(join(AUTH_DIR, 'pairing_code.ready'), '1')
      } catch (e) {
        console.error('[bridge] Failed to get pairing code:', e.message)
      }
    }, 3000)
  }

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr && !PHONE_NUMBER) {
      reconnectAttempts = 0  // reset — QR refresh is expected, not a failure
      console.log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
      console.log('  Scan this QR code with WhatsApp:')
      console.log('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n')
      qrcodeTerminal.generate(qr, { small: true })
      console.log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n')

      // Save QR as PNG so Python can send it via Telegram
      const qrPngPath = join(AUTH_DIR, 'qr.png')
      try {
        await QRCode.toFile(qrPngPath, qr, { width: 400, margin: 2 })
        // Write a ready flag so Python knows a fresh QR is available
        writeFileSync(join(AUTH_DIR, 'qr.ready'), '1')
        console.log(`[bridge] QR saved to ${qrPngPath}`)
      } catch (e) {
        console.error('[bridge] Failed to save QR PNG:', e.message)
      }
    }

    if (connection === 'open') {
      isConnected = true
      reconnectAttempts = 0
      logger.info('WhatsApp connected!')
      console.log('[bridge] WhatsApp connected! Ready to relay messages.')
    }

    if (connection === 'close') {
      isConnected = false
      const code = lastDisconnect?.error?.output?.statusCode
      const loggedOut = code === DisconnectReason.loggedOut

      if (loggedOut) {
        console.error('[bridge] Logged out. Delete ~/.jefe/wa-auth/ and restart to re-scan QR.')
        process.exit(1)
      }

      if (reconnectAttempts < MAX_RECONNECTS) {
        reconnectAttempts++
        const delay = Math.min(3000 * reconnectAttempts, 30000)
        logger.warn('Connection closed (code %d) — reconnecting in %dms (attempt %d)', code, delay, reconnectAttempts)
        setTimeout(connectToWA, delay)
      } else {
        console.error('[bridge] Max reconnect attempts reached. Exiting.')
        process.exit(1)
      }
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return

    for (const msg of messages) {
      // Skip own messages and broadcast lists
      if (msg.key.fromMe) continue
      const jid = msg.key.remoteJid
      if (!jid || jid.endsWith('@broadcast')) continue

      // Extract text from all common message types
      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        msg.message?.imageMessage?.caption ||
        msg.message?.videoMessage?.caption ||
        msg.message?.documentMessage?.caption ||
        msg.message?.buttonsResponseMessage?.selectedDisplayText ||
        ''

      const senderName = msg.pushName || jid.split('@')[0]
      const messageId  = msg.key.id

      let mediaPath = null
      let mediaType = null

      // Download image if present
      if (msg.message?.imageMessage) {
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {})
          mediaPath = join(tmpdir(), `wa_img_${Date.now()}.jpg`)
          await writeFile(mediaPath, buf)
          mediaType = 'image'
        } catch (e) {
          logger.warn('Image download failed: %s', e.message)
        }
      }

      // Download voice/audio if present
      if (msg.message?.audioMessage) {
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {})
          mediaPath = join(tmpdir(), `wa_audio_${Date.now()}.ogg`)
          await writeFile(mediaPath, buf)
          mediaType = 'audio'
        } catch (e) {
          logger.warn('Audio download failed: %s', e.message)
        }
      }

      const payload = {
        jid,
        message: text,
        sender_name: senderName,
        message_id: messageId,
        media_path: mediaPath,
        media_type: mediaType,
      }

      try {
        const headers = { 'Content-Type': 'application/json' }
        if (BRIDGE_SECRET) headers['X-WA-Bridge-Secret'] = BRIDGE_SECRET

        const resp = await fetch(BRIDGEBOT_URL, {
          method: 'POST',
          headers,
          body: JSON.stringify(payload),
        })
        if (!resp.ok) {
          logger.warn('Bridgebot rejected message from %s — HTTP %d', jid, resp.status)
        }
      } catch (e) {
        logger.error('Failed to forward to bridgebot: %s', e.message)
      }
    }
  })
}

// ── HTTP server (outbound sends + status) ────────────────────────────────────
const app = express()
app.use(express.json({ limit: '20mb' }))

app.get('/status', (_req, res) => {
  res.json({ ok: true, connected: isConnected })
})

app.post('/send', async (req, res) => {
  const { jid, message } = req.body
  if (!jid || !message) return res.status(400).json({ ok: false, error: 'jid and message required' })
  if (!isConnected || !sock) return res.status(503).json({ ok: false, error: 'WhatsApp not connected' })
  try {
    await sock.sendMessage(jid, { text: message })
    res.json({ ok: true })
  } catch (e) {
    logger.error('send failed to %s: %s', jid, e.message)
    res.status(500).json({ ok: false, error: e.message })
  }
})

app.post('/send-image', async (req, res) => {
  const { jid, path: filePath, caption } = req.body
  if (!jid || !filePath) return res.status(400).json({ ok: false, error: 'jid and path required' })
  if (!isConnected || !sock) return res.status(503).json({ ok: false, error: 'not connected' })
  try {
    const { readFile } = await import('fs/promises')
    const buffer = await readFile(filePath)
    await sock.sendMessage(jid, { image: buffer, caption: caption || '' })
    res.json({ ok: true })
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

app.post('/send-audio', async (req, res) => {
  const { jid, path: filePath } = req.body
  if (!jid || !filePath) return res.status(400).json({ ok: false, error: 'jid and path required' })
  if (!isConnected || !sock) return res.status(503).json({ ok: false, error: 'not connected' })
  try {
    const { readFile } = await import('fs/promises')
    const buffer = await readFile(filePath)
    await sock.sendMessage(jid, { audio: buffer, mimetype: 'audio/ogg; codecs=opus', ptt: true })
    res.json({ ok: true })
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

app.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[bridge] WhatsApp bridge listening on http://127.0.0.1:${BRIDGE_PORT}`)
  console.log(`[bridge] Forwarding messages to: ${BRIDGEBOT_URL}`)
})

connectToWA()
