import 'dart:async';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:typed_data';

/// Saves [bytes] through a temporary blob URL and an `<a download>` click, then
/// revokes the URL. The bytes were fetched with the in-memory bearer token, so
/// no token ever appears in a URL.
Future<void> saveBlob(Uint8List bytes, String filename, String mime) async {
  final parts = <JSAny>[bytes.toJS].toJS;
  final options = <String, String>{'type': mime}.jsify()!;
  final blob = (globalContext['Blob']! as JSFunction).callAsConstructor<JSObject>(parts, options);
  final urlApi = globalContext['URL']! as JSObject;
  final url = urlApi.callMethod<JSString>('createObjectURL'.toJS, blob);
  final doc = globalContext['document']! as JSObject;
  final a = doc.callMethod<JSObject>('createElement'.toJS, 'a'.toJS);
  a['href'] = url;
  a['download'] = filename.toJS;
  (a['style']! as JSObject)['display'] = 'none'.toJS;
  final body = doc['body']! as JSObject;
  body.callMethod<JSAny?>('appendChild'.toJS, a);
  a.callMethod<JSAny?>('click'.toJS);
  a.callMethod<JSAny?>('remove'.toJS);
  // Revoke after the browser has started the download.
  Timer(const Duration(seconds: 30), () {
    urlApi.callMethod<JSAny?>('revokeObjectURL'.toJS, url);
  });
}
