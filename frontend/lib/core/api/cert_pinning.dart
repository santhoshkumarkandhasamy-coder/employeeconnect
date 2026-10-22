import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:dio/dio.dart';
import 'package:dio/io.dart';

/// Base64(SHA-256(SPKI)) of a DER encoded X.509 certificate, or null if it
/// cannot be parsed. This is the same value as the usual
/// `openssl x509 -pubkey | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.
String? spkiPin(Uint8List certDer) {
  final spki = extractSpki(certDer);
  if (spki == null) return null;
  return base64.encode(sha256.convert(spki).bytes);
}

/// Returns the SubjectPublicKeyInfo element (tag + length + content).
Uint8List? extractSpki(Uint8List der) {
  try {
    final cert = _readHeader(der, 0);
    final tbs = _readHeader(der, cert.contentStart);
    var pos = tbs.contentStart;
    if (der[pos] == 0xA0) pos = _readHeader(der, pos).end; // [0] version
    // serialNumber, signature, issuer, validity, subject
    for (var i = 0; i < 5; i++) {
      pos = _readHeader(der, pos).end;
    }
    final spki = _readHeader(der, pos);
    return Uint8List.sublistView(der, pos, spki.end);
  } on Object {
    return null;
  }
}

class _Header {
  const _Header(this.contentStart, this.end);
  final int contentStart;
  final int end;
}

_Header _readHeader(Uint8List b, int pos) {
  var p = pos + 1; // skip tag
  var len = b[p++];
  if (len & 0x80 != 0) {
    final n = len & 0x7f;
    len = 0;
    for (var i = 0; i < n; i++) {
      len = (len << 8) | b[p++];
    }
  }
  return _Header(p, p + len);
}

bool certMatchesPins(Uint8List certDer, List<String> pins) {
  final pin = spkiPin(certDer);
  return pin != null && pins.contains(pin);
}

bool hostMatches(String host, String? expected) =>
    expected == null || expected.isEmpty || host.toLowerCase() == expected.toLowerCase();

/// Installs SPKI pinning on [dio]. With no pins configured nothing changes
/// (system trust store only) so development against a self-signed server
/// still works via normal OS trust settings.
///
/// With pins configured, system roots are NOT trusted: the handshake is only
/// accepted when the presented certificate's SPKI hash is in [pins].
///
/// When [expectedHost] is given the connection host must also equal it
/// (case-insensitive), so a pinned certificate cannot be replayed for another host.
void applyCertPinning(Dio dio, List<String> pins, {String? expectedHost}) {
  if (pins.isEmpty) return;
  dio.httpClientAdapter = IOHttpClientAdapter(
    createHttpClient: () {
      final client = HttpClient(context: SecurityContext(withTrustedRoots: false));
      client.badCertificateCallback =
          (X509Certificate cert, String host, int port) =>
              hostMatches(host, expectedHost) && certMatchesPins(cert.der, pins);
      return client;
    },
  );
}
