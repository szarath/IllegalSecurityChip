package illegal.security.chip;

import javacard.framework.APDU;
import javacard.framework.Applet;
import javacard.framework.ISO7816;
import javacard.framework.ISOException;
import javacard.framework.JCSystem;
import javacard.framework.Util;
import javacard.security.Signature;
import javacardx.apdu.ExtendedLength;

public class ISCApplet extends Applet implements ExtendedLength {
	private static final short LEN_TEMP_STATES = (short) 0x3;
	private static final short LEN_DS4RESP_SIG = JediIdentity.RSA2048_INT_SIZE;
	// Offsets
	private static final short OFFSET_TS_AUTH_CPAGE = (short) 0x0;
	private static final short OFFSET_TS_PAGE_SIZE = (short) 0x1;
	private static final short OFFSET_TS_SIG_READ_PROT = (short) 0x2;

	private static final short OFFSET_DS4RESP_SIG = (short) 0x0;
	private static final short OFFSET_DS4RESP_ID_SERIAL = OFFSET_DS4RESP_SIG + LEN_DS4RESP_SIG;
	private static final short OFFSET_DS4RESP_ID_PUB_N = OFFSET_DS4RESP_ID_SERIAL + JediIdentity.LEN_ID_SERIAL;
	private static final short OFFSET_DS4RESP_ID_PUB_E = OFFSET_DS4RESP_ID_PUB_N + JediIdentity.LEN_ID_PUB_N;
	private static final short OFFSET_DS4RESP_ID_SIG = OFFSET_DS4RESP_ID_PUB_E + JediIdentity.LEN_ID_PUB_E;

	private static final short LEN_DS4RESP = OFFSET_DS4RESP_ID_SIG + JediIdentity.LEN_ID_SIG;

	// APDU classes
	// https://cardwerk.com/smart-card-standard-iso7816-4-section-5-basic-organizations/
	private static final byte CLA_AUTH = (byte) 0x80;
	private static final byte CLA_CONFIG = (byte) 0x90;

	// APDU commands for CLA_AUTH
	private static final byte INS_AUTH_SET_CHALLENGE = (byte) 0x44;
	private static final byte INS_AUTH_GET_RESPONSE = (byte) 0x46;
	private static final byte INS_AUTH_RESET = (byte) 0x48;

	// APDU commands for CLA_CONFIG
	private static final byte INS_CONFIG_GET_STATUS = (byte) 0x00;
	private static final byte INS_CONFIG_RESET = (byte) 0x0f;
	// Import public pages
	private static final byte INS_CONFIG_IMPORT_SERIAL = (byte) 0x10;
	private static final byte INS_CONFIG_IMPORT_PUB_N = (byte) 0x11;
	private static final byte INS_CONFIG_IMPORT_PUB_E = (byte) 0x12;
	private static final byte INS_CONFIG_IMPORT_SIG_ID = (byte) 0x13;
	// Import private pages
	private static final byte INS_CONFIG_IMPORT_PRIV_P = (byte) 0x20;
	private static final byte INS_CONFIG_IMPORT_PRIV_Q = (byte) 0x21;
	private static final byte INS_CONFIG_IMPORT_PRIV_PQ = (byte) 0x22;
	private static final byte INS_CONFIG_IMPORT_PRIV_DP1 = (byte) 0x23;
	private static final byte INS_CONFIG_IMPORT_PRIV_DQ1 = (byte) 0x24;
	// Export public pages
	private static final byte INS_CONFIG_EXPORT_SERIAL = (byte) 0x30;
	private static final byte INS_CONFIG_EXPORT_PUB_N = (byte) 0x31;
	private static final byte INS_CONFIG_EXPORT_PUB_E = (byte) 0x32;
	private static final byte INS_CONFIG_EXPORT_SIG_ID = (byte) 0x33;
	// Destructive operations. Think twice before proceeding!
	private static final byte INS_CONFIG_GEN_KEYS = (byte) 0xfd;
	private static final byte INS_CONFIG_ENTER_STEALTH_MODE = (byte) 0xfe;
	private static final byte INS_CONFIG_NUKE = (byte) 0xff;

	private Signature sigChallenge;
	private final JediIdentity id;
	private final short[] tempStates;
	private final byte[] signature;
	private boolean stealthMode;

	public ISCApplet() {
		this.sigChallenge = Signature.getInstance(Signature.ALG_RSA_SHA_256_PKCS1_PSS, false);
		this.id = new JediIdentity();
		this.tempStates = JCSystem.makeTransientShortArray(LEN_TEMP_STATES, JCSystem.CLEAR_ON_DESELECT);
		this.signature = JCSystem.makeTransientByteArray(JediIdentity.RSA2048_INT_SIZE, JCSystem.CLEAR_ON_DESELECT);
		this.stealthMode = false;
	}

	public static void install(byte[] bArray, short bOffset, byte bLength)
			throws ISOException {
		ISCApplet app = new ISCApplet();
		app.register();
	}

	/**
	 * Reset authentication-related states.
	 */
	private void reset() {
		this.sigChallenge.init(this.id.getPrivateKey(), Signature.MODE_SIGN);
		// starts from page 0
		this.tempStates[OFFSET_TS_AUTH_CPAGE] = (short) 0;
		this.tempStates[OFFSET_TS_PAGE_SIZE] = (short) 0;
		this.signatureSetReadProtect(true);
		Util.arrayFillNonAtomic(this.signature, (short) 0, (short) this.signature.length, (byte) 0);
	}

	private boolean signatureIsReadProtect() {
		return this.tempStates[OFFSET_TS_SIG_READ_PROT] != 0;
	}

	private void signatureSetReadProtect(boolean val) {
		this.tempStates[OFFSET_TS_SIG_READ_PROT] = (short) (val ? 1 : 0);
	}

	private static short min(short a, short b) {
		if (a > b) {
			return b;
		} else {
			return a;
		}
	}

	private void processAuthReset(APDU apdu) throws ISOException {
		this.reset();
	}

	/**
	 * <p>
	 * Handles the request of SetChallenge. Response will be generated after writing to the last byte.
	 * Note that P1 and P2 are only used for calculating 
	 * the offset so that the whole challenge can be written in a single extended length APDU
	 * (by e.g. setting both P1 and P2 to 0). It is also possible to change the block size
	 * during the transaction.
	 * </p>
	 * 
	 * <p>
	 * Setting offset to be out-of-bound will result in {@link ISOException ISOException} with the SW
	 * {@link ISO7816#SW_WRONG_P1P2 SW_WRONG_P1P2}. Out-of-bound writes will be ignored.
	 * </p>
	 * 
	 * @param apdu The APDU context.
	 * @throws ISOException
	 */
	private void processAuthSetChallenge(APDU apdu) throws ISOException {
		// Immediately read protect the signature area
		this.signatureSetReadProtect(true);

		byte[] buf = apdu.getBuffer();
		short rectifiedP1, rectifiedP2;

		// Rectify P1 and P2
		rectifiedP1 = (short) (buf[ISO7816.OFFSET_P1] & 0xff);
		rectifiedP2 = (short) (buf[ISO7816.OFFSET_P2] & 0xff);

		// Calculate and validate offset
		short offset = (short) (rectifiedP1 * rectifiedP2);
		if (offset < 0 || offset >= this.signature.length) {
			// Offset is out of bound. Panic.
			ISOException.throwIt(ISO7816.SW_WRONG_P1P2);
			return;
		}
		
		// Get number of total incoming bytes
		short total = apdu.getIncomingLength();

		// Calculate and validate available space for write
		short totalWritable = (short) (this.signature.length - offset);
		if (totalWritable < total) {
			total = totalWritable;
		}

		// Start reading data to the buffer
		short remaining = total;
		short bytes = apdu.setIncomingAndReceive();
		short offsetCdata = apdu.getOffsetCdata();
		while (bytes > 0) {
			if (remaining < bytes) {
				// Clamp bytes to the remaining value that we decided.
				bytes = remaining;
			}
			if (remaining > 0) {
				// Copy this chunk
				Util.arrayCopyNonAtomic(buf, offsetCdata, this.signature, offset, bytes);
				remaining -= bytes;
			}
			// Receive next chunk (or discard overflowing data)
			bytes = apdu.receiveBytes(offsetCdata);
		}

		// Just finished writing the last page. Sign the buffered pages.
		if (totalWritable == total) {
			// From JavaCard doc: The input and output buffer data may overlap.
			this.sigChallenge.sign(this.signature, (short) 0, (short) this.signature.length, this.signature, (short) 0);
			this.signatureSetReadProtect(false);
		}

		tempStates[OFFSET_TS_AUTH_CPAGE]++;
	}

	/**
	 * Handles the request of GetResponse. Note that P1 and P2 are only used for calculating 
	 * the offset so that the whole response can be read in a single extended length APDU
	 * (by e.g. setting both P1 and P2 to 0). It is also possible to change the block size
	 * during the transaction.
	 * 
	 * @param apdu The APDU context.
	 * @throws ISOException
	 */
	private void processAuthGetResponse(APDU apdu) throws ISOException {
		byte[] buf = apdu.getBuffer();

		// Rectify P1 and P2
		short rectifiedP1, rectifiedP2;
		rectifiedP1 = (short) (buf[ISO7816.OFFSET_P1] & 0xff);
		rectifiedP2 = (short) (buf[ISO7816.OFFSET_P2] & 0xff);

		// Calculate and validate offset
		short offset = (short) (rectifiedP1 * rectifiedP2);
		if (offset < 0 || offset >= LEN_DS4RESP) {
			ISOException.throwIt(ISO7816.SW_WRONG_P1P2);
			return;
		}

		// Determine actual response size.
		// Accept response size set by the host but only send until the end of the response.
		short remaining = apdu.setOutgoing();
		remaining = min(remaining, (short) (LEN_DS4RESP - offset));
		apdu.setOutgoingLength(remaining);

		// Send until we have nothing to send
		while (remaining > 0) {
			// Initialize buffer offset counter
			short bufOffset = 0;
			// Counter for total number of data written (actual chunk size)
			short chunkUsed = 0;
			// Determine chunk size
			short chunkFree = min((short) buf.length, remaining);

			// fill the buffer
			while (chunkFree > 0) {
				short copySize = 0;
				if (offset >= OFFSET_DS4RESP_SIG && offset < OFFSET_DS4RESP_ID_SERIAL) {
					// Relative offset
					short relOffset = (short) (OFFSET_DS4RESP_ID_SERIAL - offset);

					// Calculate copy size (total bytes available for chunk or payload size, whichever smaller)
					copySize = min(chunkFree, LEN_DS4RESP_SIG);

					// Populate the buffer with payload
					if (this.signatureIsReadProtect()) {
						Util.arrayFillNonAtomic(buf, bufOffset, copySize, (byte) 0);
					} else {
						Util.arrayCopyNonAtomic(this.signature, relOffset, buf, bufOffset, copySize);
					}
				} else if (offset >= OFFSET_DS4RESP_ID_SERIAL && offset < OFFSET_DS4RESP_ID_PUB_N) {
					// Relative offset
					short relOffset = (short) (OFFSET_DS4RESP_ID_PUB_N - offset);

					// Calculate copy size (total bytes available for chunk or payload size, whichever smaller)
					copySize = min(chunkFree, JediIdentity.LEN_ID_SERIAL);

					// Populate the buffer with payload
					Util.arrayCopyNonAtomic(this.id.getSerialNumber(), relOffset, buf, bufOffset, copySize);
				} else if (offset >= OFFSET_DS4RESP_ID_PUB_N && offset < OFFSET_DS4RESP_ID_PUB_E) {
					// Relative offset
					short relOffset = (short) (OFFSET_DS4RESP_ID_PUB_E - offset);

					// Calculate copy size (total bytes available for chunk or payload size, whichever smaller)
					copySize = min(chunkFree, JediIdentity.LEN_ID_PUB_N);

					// Populate the buffer with payload
					Util.arrayCopyNonAtomic(this.id.exportPublicKeyN(), relOffset, buf, bufOffset, copySize);
					this.id.finishExport();
				} else if (offset >= OFFSET_DS4RESP_ID_PUB_E && offset < OFFSET_DS4RESP_ID_SIG) {
					// Relative offset
					short relOffset = (short) (OFFSET_DS4RESP_ID_SIG - offset);

					// Calculate copy size (total bytes available for chunk or payload size, whichever smaller)
					copySize = min(chunkFree, JediIdentity.LEN_ID_PUB_E);

					// Populate the buffer with payload
					Util.arrayCopyNonAtomic(this.id.exportPublicKeyE(), relOffset, buf, bufOffset, copySize);
					this.id.finishExport();
				} else if (offset >= OFFSET_DS4RESP_ID_SIG && offset < LEN_DS4RESP) {
					// Relative offset
					short relOffset = (short) (LEN_DS4RESP - offset);

					// Calculate copy size (total bytes available for chunk or payload size, whichever smaller)
					copySize = min(chunkFree, JediIdentity.LEN_ID_SIG);

					// Populate the buffer with payload
					Util.arrayCopyNonAtomic(this.id.getIdSig(), relOffset, buf, bufOffset, copySize);
				}
				// Update buffer offset
				bufOffset += copySize;
				// Increment chunk size counter
				chunkUsed += copySize;
				// Update free space available for the chunk
				chunkFree -= copySize;

				// Update remaining bytes to send
				remaining -= copySize;
				// Update offset
				offset += copySize;
			}
			// Send the chunk when it's ready
			apdu.sendBytes((short) 0, chunkUsed);
		}
	}

	public void process(APDU apdu) throws ISOException {
		// TODO
		byte[] buf = apdu.getBuffer();
		if (apdu.isISOInterindustryCLA()) {
			if (this.selectingApplet()) {
				return;
			} else {
				ISOException.throwIt(ISO7816.SW_CLA_NOT_SUPPORTED);
				return;
			}
		}
		switch (buf[ISO7816.OFFSET_CLA]) {
//		case ISO7816.CLA_ISO7816:
//			switch (buf[ISO7816.OFFSET_INS]) {
//			case ISO7816.INS_SELECT:
//				break;
//			default:
//				ISOException.throwIt(ISO7816.SW_INS_NOT_SUPPORTED);
//			}
//			break;
		case CLA_AUTH:
			if (!this.id.isReady()) {
				ISOException.throwIt(ISO7816.SW_CONDITIONS_NOT_SATISFIED);
				return;
			}
			switch (buf[ISO7816.OFFSET_INS]) {
			case INS_AUTH_RESET:
				this.processAuthReset(apdu);
				break;
			case INS_AUTH_SET_CHALLENGE:
				this.processAuthSetChallenge(apdu);
				break;
			case INS_AUTH_GET_RESPONSE:
				this.processAuthGetResponse(apdu);
				break;
			default:
				ISOException.throwIt(ISO7816.SW_INS_NOT_SUPPORTED);
			}
			break;
		case CLA_CONFIG:
			if (this.stealthMode) {
				ISOException.throwIt(ISO7816.SW_CLA_NOT_SUPPORTED);
				break;
			}
			switch (buf[ISO7816.OFFSET_INS]) {
			// TODO
			case INS_CONFIG_GEN_KEYS:
				this.id.genKeyPair();
				break;
			// Enter stealth mode. The config interface will be locked out and can only be reset by reinstalling the applet.
			case INS_CONFIG_ENTER_STEALTH_MODE:
				this.stealthMode = true;
				break;
			case INS_CONFIG_NUKE:
				this.reset();
				this.id.nuke();
				break;
			default:
				ISOException.throwIt(ISO7816.SW_INS_NOT_SUPPORTED);
			}
			break;
		default:
			ISOException.throwIt(ISO7816.SW_CLA_NOT_SUPPORTED);
		}
	}

} // ISCApplet
