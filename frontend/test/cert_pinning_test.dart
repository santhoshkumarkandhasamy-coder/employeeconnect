import 'dart:convert';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:employee_connect/core/api/cert_pinning.dart';
import 'package:flutter_test/flutter_test.dart';

/// Builds a minimal DER "certificate": SEQ{ SEQ{ [0] ver, serial, sigAlg,
/// issuer, validity, subject, SPKI } }. Enough to exercise the extractor.
Uint8List _der(int tag, List<int> content) {
  assert(content.length < 128);
  return Uint8List.fromList([tag, content.length, ...content]);
}

void main() {
  test('extracts the SPKI element and pins its SHA-256', () {
    final spki = _der(0x30, [0x05, 0x00, 0x03, 0x01, 0x00]);
    final fields = [
      ..._der(0xA0, _der(0x02, [0x02])), // version [0]
      ..._der(0x02, [0x01]), // serial
      ..._der(0x30, [0x05, 0x00]), // sigAlg
      ..._der(0x30, [0x05, 0x00]), // issuer
      ..._der(0x30, [0x05, 0x00]), // validity
      ..._der(0x30, [0x05, 0x00]), // subject
      ...spki,
    ];
    final cert = _der(0x30, _der(0x30, fields));

    expect(extractSpki(cert), spki);
    final pin = base64.encode(sha256.convert(spki).bytes);
    expect(spkiPin(cert), pin);
    expect(certMatchesPins(cert, [pin]), isTrue);
    expect(certMatchesPins(cert, ['other']), isFalse);
  });

  test('garbage input does not throw', () {
    expect(spkiPin(Uint8List.fromList([1, 2, 3])), isNull);
  });
}
