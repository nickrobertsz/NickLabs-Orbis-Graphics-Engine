//! Minimal JSON value, parser and writer (std only). Enough for Orbis state files, reports,
//! profiles and `vulkaninfo --json` output. Numbers keep integer precision when they are integers.

use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(BTreeMap<String, Value>),
}

impl Default for Value {
    fn default() -> Self {
        Value::Null
    }
}

impl Value {
    pub fn obj() -> Value {
        Value::Obj(BTreeMap::new())
    }
    pub fn get(&self, k: &str) -> Option<&Value> {
        match self {
            Value::Obj(m) => m.get(k),
            _ => None,
        }
    }
    /// Path lookup: "a.b.c"
    pub fn at(&self, path: &str) -> Option<&Value> {
        let mut v = self;
        for k in path.split('.') {
            v = v.get(k)?;
        }
        Some(v)
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            Value::Float(f) if f.fract() == 0.0 && f.abs() < 9.0e15 => Some(*f as i64),
            Value::Str(s) => s.trim().parse().ok(),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Int(i) => Some(*i as f64),
            Value::Float(f) => Some(*f),
            Value::Str(s) => s.trim().parse().ok(),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            Value::Int(i) => Some(*i != 0),
            _ => None,
        }
    }
    pub fn as_arr(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }
    pub fn as_obj(&self) -> Option<&BTreeMap<String, Value>> {
        match self {
            Value::Obj(m) => Some(m),
            _ => None,
        }
    }
    pub fn set(&mut self, k: &str, v: impl Into<Value>) -> &mut Self {
        if let Value::Obj(m) = self {
            m.insert(k.to_string(), v.into());
        }
        self
    }
    pub fn push(&mut self, v: impl Into<Value>) {
        if let Value::Arr(a) = self {
            a.push(v.into());
        }
    }

    pub fn to_string_compact(&self) -> String {
        let mut s = String::new();
        write_value(&mut s, self, None, 0);
        s
    }
    pub fn to_string_pretty(&self) -> String {
        let mut s = String::new();
        write_value(&mut s, self, Some(1), 0);
        s.push('\n');
        s
    }
}

impl From<bool> for Value {
    fn from(b: bool) -> Self {
        Value::Bool(b)
    }
}
impl From<i64> for Value {
    fn from(i: i64) -> Self {
        Value::Int(i)
    }
}
impl From<i32> for Value {
    fn from(i: i32) -> Self {
        Value::Int(i as i64)
    }
}
impl From<u32> for Value {
    fn from(i: u32) -> Self {
        Value::Int(i as i64)
    }
}
impl From<u64> for Value {
    fn from(i: u64) -> Self {
        if i <= i64::MAX as u64 { Value::Int(i as i64) } else { Value::Float(i as f64) }
    }
}
impl From<usize> for Value {
    fn from(i: usize) -> Self {
        Value::Int(i as i64)
    }
}
impl From<f64> for Value {
    fn from(f: f64) -> Self {
        Value::Float(f)
    }
}
impl From<&str> for Value {
    fn from(s: &str) -> Self {
        Value::Str(s.to_string())
    }
}
impl From<String> for Value {
    fn from(s: String) -> Self {
        Value::Str(s)
    }
}
impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(o: Option<T>) -> Self {
        o.map(Into::into).unwrap_or(Value::Null)
    }
}
impl<T: Into<Value>> From<Vec<T>> for Value {
    fn from(v: Vec<T>) -> Self {
        Value::Arr(v.into_iter().map(Into::into).collect())
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn indent(out: &mut String, step: Option<usize>, level: usize) {
    if let Some(n) = step {
        out.push('\n');
        for _ in 0..n * level {
            out.push(' ');
        }
    }
}

fn write_value(out: &mut String, v: &Value, step: Option<usize>, level: usize) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Value::Float(f) => {
            if f.is_finite() {
                if f.fract() == 0.0 && f.abs() < 1e15 {
                    let _ = write!(out, "{:.1}", f);
                } else {
                    let _ = write!(out, "{}", f);
                }
            } else {
                out.push_str("null");
            }
        }
        Value::Str(s) => write_str(out, s),
        Value::Arr(a) => {
            if a.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                indent(out, step, level + 1);
                write_value(out, x, step, level + 1);
            }
            indent(out, step, level);
            out.push(']');
        }
        Value::Obj(m) => {
            if m.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                indent(out, step, level + 1);
                write_str(out, k);
                out.push(':');
                if step.is_some() {
                    out.push(' ');
                }
                write_value(out, x, step, level + 1);
            }
            indent(out, step, level);
            out.push('}');
        }
    }
}

#[derive(Debug)]
pub struct ParseError {
    pub pos: usize,
    pub msg: &'static str,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "JSON parse error at byte {}: {}", self.pos, self.msg)
    }
}

impl std::error::Error for ParseError {}

pub fn parse(s: &str) -> Result<Value, ParseError> {
    let mut p = Parser { b: s.as_bytes(), i: 0, depth: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(p.err("trailing characters"));
    }
    Ok(v)
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
    depth: usize,
}

impl Parser<'_> {
    fn err(&self, msg: &'static str) -> ParseError {
        ParseError { pos: self.i, msg }
    }
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\n' | b'\r' | b'\t') {
            self.i += 1;
        }
    }
    fn lit(&mut self, s: &[u8], v: Value) -> Result<Value, ParseError> {
        if self.b[self.i..].starts_with(s) {
            self.i += s.len();
            Ok(v)
        } else {
            Err(self.err("invalid literal"))
        }
    }
    fn value(&mut self) -> Result<Value, ParseError> {
        if self.i >= self.b.len() {
            return Err(self.err("unexpected end"));
        }
        match self.b[self.i] {
            b'n' => self.lit(b"null", Value::Null),
            b't' => self.lit(b"true", Value::Bool(true)),
            b'f' => self.lit(b"false", Value::Bool(false)),
            b'"' => Ok(Value::Str(self.string()?)),
            b'[' => self.array(),
            b'{' => self.object(),
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(self.err("unexpected character")),
        }
    }
    fn enter(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth > 256 {
            return Err(self.err("nesting too deep"));
        }
        Ok(())
    }
    fn array(&mut self) -> Result<Value, ParseError> {
        self.enter()?;
        self.i += 1;
        let mut a = Vec::new();
        self.ws();
        if self.i < self.b.len() && self.b[self.i] == b']' {
            self.i += 1;
            self.depth -= 1;
            return Ok(Value::Arr(a));
        }
        loop {
            self.ws();
            a.push(self.value()?);
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(self.err("expected , or ]")),
            }
        }
        self.depth -= 1;
        Ok(Value::Arr(a))
    }
    fn object(&mut self) -> Result<Value, ParseError> {
        self.enter()?;
        self.i += 1;
        let mut m = BTreeMap::new();
        self.ws();
        if self.i < self.b.len() && self.b[self.i] == b'}' {
            self.i += 1;
            self.depth -= 1;
            return Ok(Value::Obj(m));
        }
        loop {
            self.ws();
            if self.b.get(self.i) != Some(&b'"') {
                return Err(self.err("expected key"));
            }
            let k = self.string()?;
            self.ws();
            if self.b.get(self.i) != Some(&b':') {
                return Err(self.err("expected :"));
            }
            self.i += 1;
            self.ws();
            let v = self.value()?;
            m.insert(k, v);
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(self.err("expected , or }")),
            }
        }
        self.depth -= 1;
        Ok(Value::Obj(m))
    }
    fn hex4(&mut self) -> Result<u32, ParseError> {
        if self.i + 4 > self.b.len() {
            return Err(self.err("short \\u escape"));
        }
        let s = std::str::from_utf8(&self.b[self.i..self.i + 4]).map_err(|_| self.err("bad \\u"))?;
        let v = u32::from_str_radix(s, 16).map_err(|_| self.err("bad \\u"))?;
        self.i += 4;
        Ok(v)
    }
    fn string(&mut self) -> Result<String, ParseError> {
        self.i += 1;
        let mut out = Vec::new();
        loop {
            let c = *self.b.get(self.i).ok_or_else(|| self.err("unterminated string"))?;
            self.i += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = *self.b.get(self.i).ok_or_else(|| self.err("bad escape"))?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let mut cp = self.hex4()?;
                            if (0xD800..0xDC00).contains(&cp) && self.b[self.i..].starts_with(b"\\u") {
                                self.i += 2;
                                let lo = self.hex4()?;
                                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo.wrapping_sub(0xDC00) & 0x3FF);
                            }
                            let ch = char::from_u32(cp).unwrap_or('\u{FFFD}');
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return Err(self.err("bad escape")),
                    }
                }
                _ => out.push(c),
            }
        }
        String::from_utf8(out).map_err(|_| self.err("invalid UTF-8"))
    }
    fn number(&mut self) -> Result<Value, ParseError> {
        let st = self.i;
        if self.b[self.i] == b'-' {
            self.i += 1;
        }
        let mut float = false;
        while self.i < self.b.len() {
            match self.b[self.i] {
                b'0'..=b'9' => {}
                b'.' | b'e' | b'E' | b'+' | b'-' => float = true,
                _ => break,
            }
            self.i += 1;
        }
        let s = std::str::from_utf8(&self.b[st..self.i]).map_err(|_| self.err("bad number"))?;
        if !float {
            if let Ok(i) = s.parse::<i64>() {
                return Ok(Value::Int(i));
            }
        }
        s.parse::<f64>().map(Value::Float).map_err(|_| self.err("bad number"))
    }
}

/// Build an object from (key, value) pairs.
#[macro_export]
macro_rules! jobj {
    ($($k:expr => $v:expr),* $(,)?) => {{
        let mut m = std::collections::BTreeMap::new();
        $( m.insert(String::from($k), $crate::json::Value::from($v)); )*
        $crate::json::Value::Obj(m)
    }};
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let s = r#"{"a": [1, 2.5, "x\n\"y\"", true, null], "b": {"c": -3, "u": "é😀"}}"#;
        let v = parse(s).unwrap();
        assert_eq!(v.at("b.c").unwrap().as_i64(), Some(-3));
        assert_eq!(v.at("b.u").unwrap().as_str(), Some("é😀"));
        let again = parse(&v.to_string_pretty()).unwrap();
        assert_eq!(v, again);
        assert_eq!(parse(&v.to_string_compact()).unwrap(), v);
    }

    #[test]
    fn errors() {
        assert!(parse("{").is_err());
        assert!(parse("[1,]").is_err());
        assert!(parse("1 2").is_err());
        assert!(parse(&"[".repeat(300)).is_err());
    }

    #[test]
    fn macro_and_set() {
        let mut v = jobj! {"x" => 1i64, "y" => "z", "n" => Option::<i64>::None};
        v.set("w", true);
        assert_eq!(v.to_string_compact(), r#"{"n":null,"w":true,"x":1,"y":"z"}"#);
    }
}
