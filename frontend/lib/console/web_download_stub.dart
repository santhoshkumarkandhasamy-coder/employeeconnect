import 'dart:typed_data';

/// Non-web builds (tests, mobile): a browser download is not possible.
Future<void> saveBlob(Uint8List bytes, String filename, String mime) async {
  throw UnsupportedError('Browser downloads are only available in the web console.');
}
