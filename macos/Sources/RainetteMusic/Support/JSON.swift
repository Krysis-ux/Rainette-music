import Foundation

/// A JSON value that round-trips exactly.
enum JSON: Hashable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    init(_ any: Any?) {
        switch any {
        case nil, is NSNull: self = .null
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() { self = .bool(number.boolValue) }
            else { self = .number(number.doubleValue) }
        case let string as String: self = .string(string)
        case let array as [Any]: self = .array(array.map(JSON.init))
        case let object as [String: Any]: self = .object(object.mapValues(JSON.init))
        default: self = .null
        }
    }

    init?(data: Data) {
        guard let any = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) else { return nil }
        self.init(any)
    }

    var foundation: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let value): return value
        case .number(let value):
            // Whole numbers go back as integers: Python reads 3.0 and 3 differently.
            if value.rounded() == value, abs(value) < 9e15 { return Int(value) }
            return value
        case .string(let value): return value
        case .array(let value): return value.map(\.foundation)
        case .object(let value): return value.mapValues(\.foundation)
        }
    }

    func encoded() -> Data {
        (try? JSONSerialization.data(withJSONObject: foundation, options: [.fragmentsAllowed])) ?? Data("null".utf8)
    }

    subscript(key: String) -> JSON {
        if case .object(let object) = self { return object[key] ?? .null }
        return .null
    }

    subscript(index: Int) -> JSON {
        if case .array(let array) = self, array.indices.contains(index) { return array[index] }
        return .null
    }

    var string: String? {
        switch self {
        case .string(let value): return value
        case .number(let value): return Int(exactly: value).map(String.init) ?? String(value)
        default: return nil
        }
    }

    /// The value as display text, empty when absent.
    var text: String { string ?? "" }

    var double: Double? {
        switch self {
        case .number(let value): return value
        case .string(let value): return Double(value)
        case .bool(let value): return value ? 1 : 0
        default: return nil
        }
    }

    /// Rounded toward zero; nil for NaN, infinity or out of range, which a plain Int(_:) would trap on.
    var int: Int? { double.flatMap { Int(exactly: $0.rounded(.towardZero)) } }

    var bool: Bool {
        switch self {
        case .bool(let value): return value
        case .number(let value): return value != 0
        case .string(let value): return !value.isEmpty
        case .array, .object: return true
        case .null: return false
        }
    }

    var array: [JSON] {
        if case .array(let value) = self { return value }
        return []
    }

    var object: [String: JSON] {
        if case .object(let value) = self { return value }
        return [:]
    }

    var isNull: Bool { if case .null = self { return true } else { return false } }
}

typealias JSONObject = [String: JSON]

extension JSON: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByFloatLiteral,
    ExpressibleByBooleanLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral, ExpressibleByNilLiteral {
    init(stringLiteral value: String) { self = .string(value) }
    init(integerLiteral value: Int) { self = .number(Double(value)) }
    init(floatLiteral value: Double) { self = .number(value) }
    init(booleanLiteral value: Bool) { self = .bool(value) }
    init(arrayLiteral elements: JSON...) { self = .array(elements) }
    init(dictionaryLiteral elements: (String, JSON)...) { self = .object(Dictionary(elements, uniquingKeysWith: { $1 })) }
    init(nilLiteral: ()) { self = .null }
}
