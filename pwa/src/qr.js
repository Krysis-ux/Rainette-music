/* A QR decoder, because Safari has no BarcodeDetector and Safari is what every
 * iPhone Home Screen app runs on. Reads what Rainette's desktop prints: all
 * versions and masks, byte/numeric/alphanumeric, no micro-QR. */

/* ── Galois field GF(256), x^8 + x^4 + x^3 + x^2 + 1 ──────────────────────── */

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);

for (let i = 0, x = 1; i < 255; i += 1) {
	EXP[i] = x;
	LOG[x] = i;
	x <<= 1;
	if (x & 0x100) x ^= 0x11d;
}
for (let i = 255; i < 512; i += 1) EXP[i] = EXP[i - 255];

function mul(a, b) {
	return a && b ? EXP[LOG[a] + LOG[b]] : 0;
}

function div(a, b) {
	return a ? EXP[LOG[a] + 255 - LOG[b]] : 0;
}

/* ── Reed-Solomon ─────────────────────────────────────────────────────────── */

/* Polynomials here are ascending: index is the power of x. Codewords are the
 * other way round, so a byte at block index k sits at power n-1-k — that
 * mapping is the only place the two conventions meet. */

function polyEval(poly, x) {
	let value = 0;
	let power = 1;
	for (let i = 0; i < poly.length; i += 1) {
		value ^= mul(poly[i], power);
		power = mul(power, x);
	}
	return value;
}

function syndromes(block, ecCount) {
	const result = new Array(ecCount).fill(0);
	let failed = false;
	for (let i = 0; i < ecCount; i += 1) {
		let value = 0;
		for (const byte of block) value = mul(value, EXP[i]) ^ byte;
		result[i] = value;
		if (value) failed = true;
	}
	return failed ? result : null;
}

/** Berlekamp-Massey: the error locator polynomial for these syndromes. */
function errorLocator(syndrome, ecCount) {
	let locator = [1];
	let previous = [1];
	let scale = 1;
	let shift = 1;

	for (let i = 0; i < ecCount; i += 1) {
		let discrepancy = syndrome[i];
		for (let j = 1; j < locator.length; j += 1) {
			discrepancy ^= mul(locator[j], syndrome[i - j]);
		}
		if (!discrepancy) { shift += 1; continue; }

		const correction = new Array(shift + previous.length).fill(0);
		const factor = div(discrepancy, scale);
		for (let j = 0; j < previous.length; j += 1) correction[shift + j] = mul(previous[j], factor);

		const next = new Array(Math.max(locator.length, correction.length)).fill(0);
		for (let j = 0; j < next.length; j += 1) next[j] = (locator[j] || 0) ^ (correction[j] || 0);

		if (2 * (locator.length - 1) <= i) {
			previous = locator;
			scale = discrepancy;
			shift = 1;
		} else {
			shift += 1;
		}
		locator = next;
	}
	return locator;
}

/** Correct a block in place. Returns false when the damage is beyond repair. */
function correct(block, ecCount) {
	const syndrome = syndromes(block, ecCount);
	if (!syndrome) return true;

	const locator = errorLocator(syndrome, ecCount);
	const errors = locator.length - 1;
	if (errors < 1 || errors > ecCount / 2) return false;

	// Chien search. A root at x = a^-j puts an error at power j, and powers run
	// from the end of the block.
	const positions = [];
	for (let power = 0; power < block.length; power += 1) {
		if (polyEval(locator, EXP[(255 - (power % 255)) % 255]) === 0) positions.push(power);
	}
	if (positions.length !== errors) return false;

	// Forney. omega = (syndromes * locator) truncated to the syndrome count.
	const omega = new Array(ecCount).fill(0);
	for (let i = 0; i < syndrome.length; i += 1) {
		for (let j = 0; j < locator.length && i + j < ecCount; j += 1) {
			omega[i + j] ^= mul(syndrome[i], locator[j]);
		}
	}
	// Over GF(2) the formal derivative keeps only the odd-power terms.
	const derivative = new Array(Math.max(1, locator.length - 1)).fill(0);
	for (let i = 1; i < locator.length; i += 2) derivative[i - 1] = locator[i];

	for (const power of positions) {
		const locate = EXP[power % 255];
		const inverse = EXP[(255 - (power % 255)) % 255];
		const denominator = polyEval(derivative, inverse);
		if (!denominator) return false;
		const index = block.length - 1 - power;
		if (index < 0 || index >= block.length) return false;
		// QR's generator starts at a^0, which is what puts the extra locator
		// factor in front of the usual omega/lambda' ratio.
		block[index] ^= mul(locate, div(polyEval(omega, inverse), denominator));
	}
	return syndromes(block, ecCount) === null;
}

/* ── Block layout ─────────────────────────────────────────────────────────── */

/* Per version and EC level: error-correction codewords per block, then the two
 * groups as [blocks, data codewords]. Straight from the specification's table,
 * and proved against generated codes rather than trusted. */
const BLOCKS = [
	[[7, 1, 19, 0, 0], [10, 1, 16, 0, 0], [13, 1, 13, 0, 0], [17, 1, 9, 0, 0]],
	[[10, 1, 34, 0, 0], [16, 1, 28, 0, 0], [22, 1, 22, 0, 0], [28, 1, 16, 0, 0]],
	[[15, 1, 55, 0, 0], [26, 1, 44, 0, 0], [18, 2, 17, 0, 0], [22, 2, 13, 0, 0]],
	[[20, 1, 80, 0, 0], [18, 2, 32, 0, 0], [26, 2, 24, 0, 0], [16, 4, 9, 0, 0]],
	[[26, 1, 108, 0, 0], [24, 2, 43, 0, 0], [18, 2, 15, 2, 16], [22, 2, 11, 2, 12]],
	[[18, 2, 68, 0, 0], [16, 4, 27, 0, 0], [24, 4, 19, 0, 0], [28, 4, 15, 0, 0]],
	[[20, 2, 78, 0, 0], [18, 4, 31, 0, 0], [18, 2, 14, 4, 15], [26, 4, 13, 1, 14]],
	[[24, 2, 97, 0, 0], [22, 2, 38, 2, 39], [22, 4, 18, 2, 19], [26, 4, 14, 2, 15]],
	[[30, 2, 116, 0, 0], [22, 3, 36, 2, 37], [20, 4, 16, 4, 17], [24, 4, 12, 4, 13]],
	[[18, 2, 68, 2, 69], [26, 4, 43, 1, 44], [24, 6, 19, 2, 20], [28, 6, 15, 2, 16]],
	[[20, 4, 81, 0, 0], [30, 1, 50, 4, 51], [28, 4, 22, 4, 23], [24, 3, 12, 8, 13]],
	[[24, 2, 92, 2, 93], [22, 6, 36, 2, 37], [26, 4, 20, 6, 21], [28, 7, 14, 4, 15]],
	[[26, 4, 107, 0, 0], [22, 8, 37, 1, 38], [24, 8, 20, 4, 21], [22, 12, 11, 4, 12]],
	[[30, 3, 115, 1, 116], [24, 4, 40, 5, 41], [20, 11, 16, 5, 17], [24, 11, 12, 5, 13]],
	[[22, 5, 87, 1, 88], [24, 5, 41, 5, 42], [30, 5, 24, 7, 25], [24, 11, 12, 7, 13]],
	[[24, 5, 98, 1, 99], [28, 7, 45, 3, 46], [24, 15, 19, 2, 20], [30, 3, 15, 13, 16]],
	[[28, 1, 107, 5, 108], [28, 10, 46, 1, 47], [28, 1, 22, 15, 23], [28, 2, 14, 17, 15]],
	[[30, 5, 120, 1, 121], [26, 9, 43, 4, 44], [28, 17, 22, 1, 23], [28, 2, 14, 19, 15]],
	[[28, 3, 113, 4, 114], [26, 3, 44, 11, 45], [26, 17, 21, 4, 22], [26, 9, 13, 16, 14]],
	[[28, 3, 107, 5, 108], [26, 3, 41, 13, 42], [30, 15, 24, 5, 25], [28, 15, 15, 10, 16]],
	[[28, 4, 116, 4, 117], [26, 17, 42, 0, 0], [28, 17, 22, 6, 23], [30, 19, 16, 6, 17]],
	[[28, 2, 111, 7, 112], [28, 17, 46, 0, 0], [30, 7, 24, 16, 25], [24, 34, 13, 0, 0]],
	[[30, 4, 121, 5, 122], [28, 4, 47, 14, 48], [30, 11, 24, 14, 25], [30, 16, 15, 14, 16]],
	[[30, 6, 117, 4, 118], [28, 6, 45, 14, 46], [30, 11, 24, 16, 25], [30, 30, 16, 2, 17]],
	[[26, 8, 106, 4, 107], [28, 8, 47, 13, 48], [30, 7, 24, 22, 25], [30, 22, 15, 13, 16]],
	[[28, 10, 114, 2, 115], [28, 19, 46, 4, 47], [28, 28, 22, 6, 23], [30, 33, 16, 4, 17]],
	[[30, 8, 122, 4, 123], [28, 22, 45, 3, 46], [30, 8, 23, 26, 24], [30, 12, 15, 28, 16]],
	[[30, 3, 117, 10, 118], [28, 3, 45, 23, 46], [30, 4, 24, 31, 25], [30, 11, 15, 31, 16]],
	[[30, 7, 116, 7, 117], [28, 21, 45, 7, 46], [30, 1, 23, 37, 24], [30, 19, 15, 26, 16]],
	[[30, 5, 115, 10, 116], [28, 19, 47, 10, 48], [30, 15, 24, 25, 25], [30, 23, 15, 25, 16]],
	[[30, 13, 115, 3, 116], [28, 2, 46, 29, 47], [30, 42, 24, 1, 25], [30, 23, 15, 28, 16]],
	[[30, 17, 115, 0, 0], [28, 10, 46, 23, 47], [30, 10, 24, 35, 25], [30, 19, 15, 35, 16]],
	[[30, 17, 115, 1, 116], [28, 14, 46, 21, 47], [30, 29, 24, 19, 25], [30, 11, 15, 46, 16]],
	[[30, 13, 115, 6, 116], [28, 14, 46, 23, 47], [30, 44, 24, 7, 25], [30, 59, 16, 1, 17]],
	[[30, 12, 121, 7, 122], [28, 12, 47, 26, 48], [30, 39, 24, 14, 25], [30, 22, 15, 41, 16]],
	[[30, 6, 121, 14, 122], [28, 6, 47, 34, 48], [30, 46, 24, 10, 25], [30, 2, 15, 64, 16]],
	[[30, 17, 122, 4, 123], [28, 29, 46, 14, 47], [30, 49, 24, 10, 25], [30, 24, 15, 46, 16]],
	[[30, 4, 122, 18, 123], [28, 13, 46, 32, 47], [30, 48, 24, 14, 25], [30, 42, 15, 32, 16]],
	[[30, 20, 117, 4, 118], [28, 40, 47, 7, 48], [30, 43, 24, 22, 25], [30, 10, 15, 67, 16]],
	[[30, 19, 118, 6, 119], [28, 18, 47, 31, 48], [30, 34, 24, 34, 25], [30, 20, 15, 61, 16]],
];

/* Alignment pattern row/column centres, by version. Every pairing of these is a
 * pattern centre except the three that collide with a finder. */
const ALIGNMENT = [
	[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
	[6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62], [6, 26, 46, 66],
	[6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78], [6, 30, 56, 82], [6, 30, 58, 86],
	[6, 34, 62, 90], [6, 28, 50, 72, 94], [6, 26, 50, 74, 98], [6, 30, 54, 78, 102],
	[6, 28, 54, 80, 106], [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
	[6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126], [6, 26, 52, 78, 104, 130],
	[6, 30, 56, 82, 108, 134], [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142],
	[6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150], [6, 24, 50, 76, 102, 128, 154],
	[6, 28, 54, 80, 106, 132, 158], [6, 32, 58, 84, 110, 136, 162],
	[6, 26, 54, 82, 110, 138, 166], [6, 30, 58, 86, 114, 142, 170],
];

/* EC level as encoded in the format bits (01=L, 00=M, 11=Q, 10=H), mapped to the
 * order the block table uses. */
const EC_ORDER = { 1: 0, 0: 1, 3: 2, 2: 3 };

const MASKS = [
	(row, column) => (row + column) % 2 === 0,
	row => row % 2 === 0,
	(_row, column) => column % 3 === 0,
	(row, column) => (row + column) % 3 === 0,
	(row, column) => (Math.floor(row / 2) + Math.floor(column / 3)) % 2 === 0,
	(row, column) => ((row * column) % 2) + ((row * column) % 3) === 0,
	(row, column) => (((row * column) % 2) + ((row * column) % 3)) % 2 === 0,
	(row, column) => (((row + column) % 2) + ((row * column) % 3)) % 2 === 0,
];

/* ── The module grid ──────────────────────────────────────────────────────── */

class Grid {
	constructor(size) {
		this.size = size;
		this.bits = new Uint8Array(size * size);
	}

	get(row, column) {
		return this.bits[row * this.size + column];
	}

	set(row, column, value) {
		this.bits[row * this.size + column] = value ? 1 : 0;
	}
}

/** Every module a decoder must skip: finders, timing, format, version, alignment. */
function functionModules(size, version) {
	const reserved = new Uint8Array(size * size);
	const mark = (row, column, width, height) => {
		for (let r = row; r < row + height; r += 1) {
			for (let c = column; c < column + width; c += 1) {
				if (r >= 0 && r < size && c >= 0 && c < size) reserved[r * size + c] = 1;
			}
		}
	};

	mark(0, 0, 9, 9);
	mark(0, size - 8, 8, 9);
	mark(size - 8, 0, 9, 8);
	for (let i = 0; i < size; i += 1) {
		reserved[6 * size + i] = 1;
		reserved[i * size + 6] = 1;
	}

	const centres = ALIGNMENT[version - 1] || [];
	for (const row of centres) {
		for (const column of centres) {
			const nearFinder =
				(row <= 8 && column <= 8) ||
				(row <= 8 && column >= size - 9) ||
				(row >= size - 9 && column <= 8);
			if (nearFinder) continue;
			mark(row - 2, column - 2, 5, 5);
		}
	}

	if (version >= 7) {
		mark(0, size - 11, 3, 6);
		mark(size - 11, 0, 6, 3);
	}
	return reserved;
}

function bchFormatDistance(a, b) {
	let difference = a ^ b;
	let bits = 0;
	while (difference) { bits += difference & 1; difference >>= 1; }
	return bits;
}

/* The 32 legal format words, generated rather than tabulated. */
const FORMAT_WORDS = (() => {
	const words = [];
	for (let data = 0; data < 32; data += 1) {
		let value = data << 10;
		for (let i = 4; i >= 0; i -= 1) {
			if (value & (1 << (i + 10))) value ^= 0x537 << i;
		}
		words.push({ data, word: ((data << 10) | value) ^ 0x5412 });
	}
	return words;
})();

function readFormat(grid) {
	const size = grid.size;
	const read = coordinates => coordinates.reduce((bits, [row, column]) => (bits << 1) | grid.get(row, column), 0);

	const first = read([
		[8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8],
		[7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8],
	]);
	const second = read([
		[size - 1, 8], [size - 2, 8], [size - 3, 8], [size - 4, 8], [size - 5, 8],
		[size - 6, 8], [size - 7, 8], [8, size - 8], [8, size - 7], [8, size - 6],
		[8, size - 5], [8, size - 4], [8, size - 3], [8, size - 2], [8, size - 1],
	]);

	let best = null;
	for (const candidate of [first, second]) {
		for (const { data, word } of FORMAT_WORDS) {
			const distance = bchFormatDistance(candidate, word);
			if (!best || distance < best.distance) best = { distance, data };
		}
	}
	if (!best || best.distance > 3) return null;
	return { ec: (best.data >> 3) & 3, mask: best.data & 7 };
}

/** Walk the zig-zag data path and lift the codewords out. */
function readCodewords(grid, version, mask) {
	const size = grid.size;
	const reserved = functionModules(size, version);
	const unmask = MASKS[mask];
	const bytes = [];
	let current = 0;
	let bits = 0;

	let upward = true;
	for (let pair = size - 1; pair > 0; pair -= 2) {
		if (pair === 6) pair -= 1;   // the vertical timing column is not data
		for (let step = 0; step < size; step += 1) {
			const row = upward ? size - 1 - step : step;
			for (const column of [pair, pair - 1]) {
				if (reserved[row * size + column]) continue;
				let bit = grid.get(row, column);
				if (unmask(row, column)) bit ^= 1;
				current = (current << 1) | bit;
				bits += 1;
				if (bits === 8) { bytes.push(current); current = 0; bits = 0; }
			}
		}
		upward = !upward;
	}
	return bytes;
}

/** Undo the interleaving, then error-correct each block. */
function repairBlocks(codewords, version, ec) {
	const [ecPerBlock, group1, data1, group2, data2] = BLOCKS[version - 1][EC_ORDER[ec]];
	const layout = [];
	for (let i = 0; i < group1; i += 1) layout.push(data1);
	for (let i = 0; i < group2; i += 1) layout.push(data2);

	const totalBlocks = layout.length;
	const blocks = layout.map(size => new Uint8Array(size + ecPerBlock));
	const longest = Math.max(...layout);

	let cursor = 0;
	for (let column = 0; column < longest; column += 1) {
		for (let block = 0; block < totalBlocks; block += 1) {
			if (column >= layout[block]) continue;
			blocks[block][column] = codewords[cursor];
			cursor += 1;
		}
	}
	for (let column = 0; column < ecPerBlock; column += 1) {
		for (let block = 0; block < totalBlocks; block += 1) {
			blocks[block][layout[block] + column] = codewords[cursor];
			cursor += 1;
		}
	}

	const data = [];
	for (const [index, block] of blocks.entries()) {
		if (!correct(block, ecPerBlock)) return null;
		for (let i = 0; i < layout[index]; i += 1) data.push(block[i]);
	}
	return data;
}

/* ── Segments ─────────────────────────────────────────────────────────────── */

const ALPHANUMERIC = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:';

function countBits(mode, version) {
	const tier = version <= 9 ? 0 : version <= 26 ? 1 : 2;
	if (mode === 1) return [10, 12, 14][tier];
	if (mode === 2) return [9, 11, 13][tier];
	if (mode === 4) return [8, 16, 16][tier];
	if (mode === 8) return [8, 10, 12][tier];
	return 0;
}

function decodeSegments(data, version) {
	let position = 0;
	const total = data.length * 8;
	const read = count => {
		let value = 0;
		for (let i = 0; i < count; i += 1) {
			if (position >= total) throw new Error('ran off the end of the data');
			const byte = data[position >> 3];
			value = (value << 1) | ((byte >> (7 - (position & 7))) & 1);
			position += 1;
		}
		return value;
	};

	const bytes = [];
	let text = '';
	while (position + 4 <= total) {
		const mode = read(4);
		if (mode === 0) break;   // terminator
		const count = read(countBits(mode, version));

		if (mode === 4) {
			for (let i = 0; i < count; i += 1) bytes.push(read(8));
			continue;
		}
		// A non-byte segment ends any byte run, so decode what we have in order.
		if (bytes.length) { text += new TextDecoder().decode(new Uint8Array(bytes)); bytes.length = 0; }

		if (mode === 1) {
			let remaining = count;
			while (remaining >= 3) { text += String(read(10)).padStart(3, '0'); remaining -= 3; }
			if (remaining === 2) text += String(read(7)).padStart(2, '0');
			else if (remaining === 1) text += String(read(4));
		} else if (mode === 2) {
			let remaining = count;
			while (remaining >= 2) {
				const pair = read(11);
				text += ALPHANUMERIC[Math.floor(pair / 45)] + ALPHANUMERIC[pair % 45];
				remaining -= 2;
			}
			if (remaining === 1) text += ALPHANUMERIC[read(6)];
		} else if (mode === 7) {
			read(8);   // ECI assignment: UTF-8 is assumed either way
		} else {
			throw new Error(`unsupported segment mode ${mode}`);
		}
	}
	if (bytes.length) text += new TextDecoder().decode(new Uint8Array(bytes));
	return text;
}

/** Read a grid that is already square, upright and one bit per module. */
export function decodeGrid(grid) {
	const version = (grid.size - 17) / 4;
	if (!Number.isInteger(version) || version < 1 || version > 40) return null;
	const format = readFormat(grid);
	if (!format) return null;
	const codewords = readCodewords(grid, version, format.mask);
	const data = repairBlocks(codewords, version, format.ec);
	if (!data) return null;
	try {
		return decodeSegments(data, version);
	} catch {
		return null;
	}
}

export { Grid };

/* ── Finding a code in a camera frame ─────────────────────────────────────── */

/* A phone camera gives uneven light across the frame — a bright screen, a dark
 * room, a hand's shadow — so one threshold for the whole image loses a corner.
 * Thresholding per tile against that tile's own range keeps all four. */
const TILE = 16;

function toLuma(data, width, height) {
	const luma = new Uint8Array(width * height);
	for (let i = 0, p = 0; i < luma.length; i += 1, p += 4) {
		luma[i] = (data[p] * 77 + data[p + 1] * 150 + data[p + 2] * 29) >> 8;
	}
	return luma;
}

function binarize(luma, width, height) {
	const bits = new Uint8Array(width * height);
	const tilesX = Math.ceil(width / TILE);
	const tilesY = Math.ceil(height / TILE);
	const thresholds = new Uint8Array(tilesX * tilesY);

	for (let ty = 0; ty < tilesY; ty += 1) {
		for (let tx = 0; tx < tilesX; tx += 1) {
			let min = 255;
			let max = 0;
			let sum = 0;
			let count = 0;
			for (let y = ty * TILE; y < Math.min((ty + 1) * TILE, height); y += 1) {
				for (let x = tx * TILE; x < Math.min((tx + 1) * TILE, width); x += 1) {
					const value = luma[y * width + x];
					if (value < min) min = value;
					if (value > max) max = value;
					sum += value;
					count += 1;
				}
			}
			// A flat tile is all paper or all ink; judging it on its own range
			// would turn sensor noise into a checkerboard.
			thresholds[ty * tilesX + tx] = max - min > 24 ? (sum / count) : (min > 127 ? 0 : 255);
		}
	}

	for (let y = 0; y < height; y += 1) {
		const ty = Math.min(tilesY - 1, y >> 4);
		for (let x = 0; x < width; x += 1) {
			const tx = Math.min(tilesX - 1, x >> 4);
			// Average the neighbouring tiles so a tile edge is not a visible seam.
			let sum = 0;
			let count = 0;
			for (let dy = -1; dy <= 1; dy += 1) {
				for (let dx = -1; dx <= 1; dx += 1) {
					const ny = ty + dy;
					const nx = tx + dx;
					if (ny < 0 || nx < 0 || ny >= tilesY || nx >= tilesX) continue;
					sum += thresholds[ny * tilesX + nx];
					count += 1;
				}
			}
			bits[y * width + x] = luma[y * width + x] < sum / count ? 1 : 0;
		}
	}
	return bits;
}

/* A finder reads 1:1:3:1:1 through its middle; an alignment bullseye reads
 * 1:1:1:1:1. Same run test, different shape. */
const FINDER_RATIO = [1, 1, 3, 1, 1];
const BULLSEYE_RATIO = [1, 1, 1, 1, 1];

function matchesRatio(runs, ratio) {
	const total = runs[0] + runs[1] + runs[2] + runs[3] + runs[4];
	const weight = ratio[0] + ratio[1] + ratio[2] + ratio[3] + ratio[4];
	if (total < weight) return false;
	const unit = total / weight;
	const slack = unit / 1.6;
	return ratio.every((share, index) => Math.abs(unit * share - runs[index]) < slack * share);
}

function isFinderRatio(runs) {
	return matchesRatio(runs, FINDER_RATIO);
}

function crossesCentre(bits, width, height, x, y, horizontal, ratio = FINDER_RATIO) {
	const limit = horizontal ? width : height;
	const at = offset => (horizontal ? bits[y * width + offset] : bits[offset * width + x]);
	let start = horizontal ? x : y;
	if (!at(start)) return null;

	const runs = [0, 0, 0, 0, 0];
	let position = start;
	while (position >= 0 && at(position)) { runs[2] += 1; position -= 1; }
	while (position >= 0 && !at(position)) { runs[1] += 1; position -= 1; }
	while (position >= 0 && at(position)) { runs[0] += 1; position -= 1; }

	position = start + 1;
	while (position < limit && at(position)) { runs[2] += 1; position += 1; }
	while (position < limit && !at(position)) { runs[3] += 1; position += 1; }
	while (position < limit && at(position)) { runs[4] += 1; position += 1; }

	if (!runs[0] || !runs[1] || !runs[3] || !runs[4]) return null;
	return matchesRatio(runs, ratio) ? { size: runs.reduce((a, b) => a + b, 0) } : null;
}

/** Run-length encode one row as `{ dark, start, length }`. */
function encodeRow(bits, width, y) {
	const runs = [];
	let start = 0;
	let dark = bits[y * width];
	for (let x = 1; x <= width; x += 1) {
		const value = x < width ? bits[y * width + x] : -1;
		if (value === dark) continue;
		runs.push({ dark: dark === 1, start, length: x - start });
		start = x;
		dark = value;
	}
	return runs;
}

function findFinders(bits, width, height) {
	const found = [];

	for (let y = 0; y < height; y += 2) {
		const runs = encodeRow(bits, width, y);
		for (let i = 0; i + 4 < runs.length; i += 1) {
			if (!runs[i].dark) continue;   // the pattern starts on ink
			const window = [runs[i], runs[i + 1], runs[i + 2], runs[i + 3], runs[i + 4]];
			if (!isFinderRatio(window.map(run => run.length))) continue;
			const centreX = window[2].start + window[2].length / 2;
			// A row can cut any horizontal band; only a real finder also reads
			// 1:1:3:1:1 straight down its own column.
			if (!crossesCentre(bits, width, height, Math.round(centreX), y, false)) continue;
			found.push({
				x: centreX,
				y,
				size: window.reduce((sum, run) => sum + run.length, 0) / 7,
			});
		}
	}

	// One pattern is seen on many rows; merge the sightings into one centre.
	const clusters = [];
	for (const point of found) {
		const near = clusters.find(cluster =>
			Math.abs(cluster.x - point.x) < cluster.size * 2 && Math.abs(cluster.y - point.y) < cluster.size * 2);
		if (near) {
			near.x = (near.x * near.count + point.x) / (near.count + 1);
			near.y = (near.y * near.count + point.y) / (near.count + 1);
			near.size = (near.size * near.count + point.size) / (near.count + 1);
			near.count += 1;
		} else {
			clusters.push({ ...point, count: 1 });
		}
	}
	return clusters.filter(cluster => cluster.count >= 2).sort((a, b) => b.count - a.count).slice(0, 6);
}

function distance(a, b) {
	return Math.hypot(a.x - b.x, a.y - b.y);
}

/* The corner between the two shortest sides is the top-left; the winding order
 * of the other two says which is which. */
function orderFinders([a, b, c]) {
	const sides = [
		{ length: distance(b, c), opposite: a, ends: [b, c] },
		{ length: distance(a, c), opposite: b, ends: [a, c] },
		{ length: distance(a, b), opposite: c, ends: [a, b] },
	].sort((x, y) => y.length - x.length);

	const topLeft = sides[0].opposite;
	let [first, second] = sides[0].ends;
	const cross = (first.x - topLeft.x) * (second.y - topLeft.y) - (first.y - topLeft.y) * (second.x - topLeft.x);
	if (cross < 0) [first, second] = [second, first];
	return { topLeft, topRight: first, bottomLeft: second };
}

/* ── Perspective sampling ─────────────────────────────────────────────────── */

function squareToQuad(x0, y0, x1, y1, x2, y2, x3, y3) {
	const dx3 = x0 - x1 + x2 - x3;
	const dy3 = y0 - y1 + y2 - y3;
	if (dx3 === 0 && dy3 === 0) {
		return [x1 - x0, x2 - x1, x0, y1 - y0, y2 - y1, y0, 0, 0, 1];
	}
	const dx1 = x1 - x2;
	const dx2 = x3 - x2;
	const dy1 = y1 - y2;
	const dy2 = y3 - y2;
	const denominator = dx1 * dy2 - dx2 * dy1;
	if (!denominator) return null;
	const a13 = (dx3 * dy2 - dx2 * dy3) / denominator;
	const a23 = (dx1 * dy3 - dx3 * dy1) / denominator;
	return [
		x1 - x0 + a13 * x1, x3 - x0 + a23 * x3, x0,
		y1 - y0 + a13 * y1, y3 - y0 + a23 * y3, y0,
		a13, a23, 1,
	];
}

function adjoint(m) {
	return [
		m[4] * m[8] - m[5] * m[7], m[2] * m[7] - m[1] * m[8], m[1] * m[5] - m[2] * m[4],
		m[5] * m[6] - m[3] * m[8], m[0] * m[8] - m[2] * m[6], m[2] * m[3] - m[0] * m[5],
		m[3] * m[7] - m[4] * m[6], m[1] * m[6] - m[0] * m[7], m[0] * m[4] - m[1] * m[3],
	];
}

function multiply(a, b) {
	const out = new Array(9).fill(0);
	for (let row = 0; row < 3; row += 1) {
		for (let column = 0; column < 3; column += 1) {
			let sum = 0;
			for (let k = 0; k < 3; k += 1) sum += a[row * 3 + k] * b[k * 3 + column];
			out[row * 3 + column] = sum;
		}
	}
	return out;
}

function apply(m, x, y) {
	const w = m[6] * x + m[7] * y + m[8];
	return [(m[0] * x + m[1] * y + m[2]) / w, (m[3] * x + m[4] * y + m[5]) / w];
}

/** Sample `size` × `size` modules out of the image through a projective fit. */
function sample(bits, width, height, size, from, to) {
	const source = squareToQuad(...from);
	const destination = squareToQuad(...to);
	if (!source || !destination) return null;
	const transform = multiply(destination, adjoint(source));

	const grid = new Grid(size);
	for (let row = 0; row < size; row += 1) {
		for (let column = 0; column < size; column += 1) {
			const [x, y] = apply(transform, column + 0.5, row + 0.5);
			const px = Math.round(x);
			const py = Math.round(y);
			if (px < 0 || py < 0 || px >= width || py >= height) return null;
			grid.set(row, column, bits[py * width + px]);
		}
	}
	return grid;
}

/* An alignment pattern is a 5-module bullseye. Searching only near where the
 * three finders predict it keeps this cheap and stops a stray mark from
 * bending the transform. */
function findAlignment(bits, width, height, guessX, guessY, moduleSize) {
	const radius = Math.max(4, Math.round(moduleSize * 4));
	let best = null;
	for (let y = Math.max(1, Math.round(guessY - radius)); y < Math.min(height - 1, guessY + radius); y += 1) {
		for (let x = Math.max(1, Math.round(guessX - radius)); x < Math.min(width - 1, guessX + radius); x += 1) {
			if (!bits[y * width + x]) continue;
			const horizontal = crossesCentre(bits, width, height, x, y, true, BULLSEYE_RATIO);
			if (!horizontal) continue;
			if (Math.abs(horizontal.size / 5 - moduleSize) > moduleSize / 2) continue;
			if (!crossesCentre(bits, width, height, x, y, false, BULLSEYE_RATIO)) continue;
			const score = Math.hypot(x - guessX, y - guessY);
			if (!best || score < best.score) best = { x, y, score };
		}
	}
	return best;
}

/* Finders are hunted along horizontal rows, so a code turned much past 15
 * degrees stops reading, and a finder square repeats every quarter turn. */
const RETRY_ANGLES = [Math.PI / 8, Math.PI / 4, (3 * Math.PI) / 8];

function rotate(bits, width, height, angle) {
	const cos = Math.cos(angle);
	const sin = Math.sin(angle);
	const size = Math.ceil(width * Math.abs(cos) + height * Math.abs(sin));
	const turned = new Uint8Array(size * size);
	const centreX = width / 2;
	const centreY = height / 2;
	for (let y = 0; y < size; y += 1) {
		const dy = y - size / 2;
		for (let x = 0; x < size; x += 1) {
			const dx = x - size / 2;
			const sourceX = Math.round(centreX + dx * cos + dy * sin);
			const sourceY = Math.round(centreY - dx * sin + dy * cos);
			if (sourceX < 0 || sourceY < 0 || sourceX >= width || sourceY >= height) continue;
			turned[y * size + x] = bits[sourceY * width + sourceX];
		}
	}
	return { bits: turned, width: size, height: size };
}

/** Read a QR code from one camera frame. Null means this frame had none, which
 *  on a live stream is ordinary rather than an error. */
export function decodeImage(imageData) {
	const { width, height, data } = imageData;
	const luma = toLuma(data, width, height);
	const upright = binarize(luma, width, height);

	const straight = readBitmap(upright, width, height);
	if (straight !== null) return straight;

	for (const angle of RETRY_ANGLES) {
		const turned = rotate(upright, width, height, angle);
		const text = readBitmap(turned.bits, turned.width, turned.height);
		if (text !== null) return text;
	}
	return null;
}

function readBitmap(bits, width, height) {
	const candidates = findFinders(bits, width, height);
	if (candidates.length < 3) return null;

	// Try the strongest triples rather than only the single best one.
	const triples = [];
	for (let a = 0; a < candidates.length; a += 1) {
		for (let b = a + 1; b < candidates.length; b += 1) {
			for (let c = b + 1; c < candidates.length; c += 1) {
				triples.push([candidates[a], candidates[b], candidates[c]]);
				if (triples.length >= 10) break;
			}
		}
	}

	for (const triple of triples) {
		const text = readTriple(bits, width, height, triple);
		if (text) return text;
	}
	return null;
}

function readTriple(bits, width, height, triple) {
	const { topLeft, topRight, bottomLeft } = orderFinders(triple);
	const moduleSize = (topLeft.size + topRight.size + bottomLeft.size) / 3;
	if (!(moduleSize > 0.7)) return null;

	const across = (distance(topLeft, topRight) + distance(topLeft, bottomLeft)) / 2;
	let estimated = Math.round(across / moduleSize) + 7;
	// Every version is 4n+17 modules, so the estimate is nudged to the nearest
	// legal size; being two out means the estimate was not close enough to fix.
	if (estimated % 4 === 0) estimated += 1;
	else if (estimated % 4 === 2) estimated -= 1;
	else if (estimated % 4 === 3) return null;

	// Steep foreshortening skews the module size enough to land a whole version
	// out. A neighbouring size costs one more sample and the format check
	// rejects a wrong guess outright, so trying is cheaper than failing.
	for (const size of [estimated, estimated - 4, estimated + 4]) {
		if (size < 21 || size > 177) continue;
		const text = readAtSize(bits, width, height, { topLeft, topRight, bottomLeft }, size, moduleSize);
		if (text !== null) return text;
	}
	return null;
}

function readAtSize(bits, width, height, { topLeft, topRight, bottomLeft }, size, moduleSize) {
	const version = (size - 17) / 4;
	const estimate = [
		topRight.x + bottomLeft.x - topLeft.x,
		topRight.y + bottomLeft.y - topLeft.y,
	];

	/* Two readings of the fourth corner. The alignment pattern is the accurate
	 * one when the code is tilted, but a bullseye picked out of background clutter
	 * is worse than none, so the plain estimate stays as a fallback rather than
	 * being replaced by it. */
	const attempts = [{
		from: [3.5, 3.5, size - 3.5, 3.5, size - 3.5, size - 3.5, 3.5, size - 3.5],
		corner: estimate,
	}];

	if (version >= 2) {
		const centres = ALIGNMENT[version - 1];
		const last = centres[centres.length - 1];
		// The bullseye sits 3 modules inside the estimated corner, along the
		// diagonal from the top-left finder.
		const ratio = 1 - 3 / (size - 7);
		const found = findAlignment(
			bits, width, height,
			topLeft.x + ratio * (estimate[0] - topLeft.x),
			topLeft.y + ratio * (estimate[1] - topLeft.y),
			moduleSize,
		);
		if (found) {
			attempts.unshift({
				from: [3.5, 3.5, size - 3.5, 3.5, last + 0.5, last + 0.5, 3.5, size - 3.5],
				corner: [found.x, found.y],
			});
		}
	}

	for (const attempt of attempts) {
		const grid = sample(bits, width, height, size, attempt.from, [
			topLeft.x, topLeft.y,
			topRight.x, topRight.y,
			attempt.corner[0], attempt.corner[1],
			bottomLeft.x, bottomLeft.y,
		]);
		const text = grid ? decodeGrid(grid) : null;
		if (text !== null) return text;
	}
	return null;
}
