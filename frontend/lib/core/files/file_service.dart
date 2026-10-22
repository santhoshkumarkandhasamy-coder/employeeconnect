import 'dart:collection';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/dio_client.dart';
import '../../features/auth/presentation/auth_controller.dart';
import '../../features/auth/presentation/auth_state.dart';

/// Purposes accepted by `POST /files` (api-contract section 2).
abstract final class FilePurpose {
  static const announcementImage = 'announcement_image';
  static const announcementPdf = 'announcement_pdf';
  // Profile photos go only through POST /me/profile/photo (review 07 L4).
  static const leaveAttachment = 'leave_attachment';
}

/// Extracts the file id from `/api/files/<id>` (or a bare `/files/<id>`).
/// The app talks to files through its own base URL, never through the URL the
/// server returned, so a tampered link cannot send the bearer token elsewhere.
String? fileIdFromUrl(String? url) {
  if (url == null || url.isEmpty) return null;
  final path = Uri.tryParse(url)?.path ?? url;
  final m = RegExp(r'/files/([A-Za-z0-9-]{8,64})/?$').firstMatch(path);
  return m?[1];
}

/// Authenticated file access through the app Dio (bearer, refresh, pinning).
/// Images are loaded as bytes, never with `Image.network`, because the server
/// requires the Authorization header and answers `no-store`.
class FileService {
  FileService(this._dio, {this.cacheLimit = 24});
  final Dio _dio;
  final int cacheLimit;

  // Small in-memory LRU (never on disk). Dropped on logout / lock.
  final LinkedHashMap<String, Uint8List> _mem = LinkedHashMap();

  void clearMemory() => _mem.clear();

  /// Bytes of `GET /files/{id}`; [url] is the `/api/files/<id>` link.
  Future<Uint8List> bytes(String url) async {
    final id = fileIdFromUrl(url);
    if (id == null) {
      throw const FormatException('Not a file link');
    }
    final hit = _mem.remove(id);
    if (hit != null) {
      _mem[id] = hit;
      return hit;
    }
    final res = await guardApi(
      () => _dio.get<List<int>>(
        '/files/$id',
        options: Options(responseType: ResponseType.bytes),
      ),
    );
    final data = Uint8List.fromList(res.data ?? const <int>[]);
    _mem[id] = data;
    while (_mem.length > cacheLimit) {
      _mem.remove(_mem.keys.first);
    }
    return data;
  }

  /// `POST /files` multipart; returns the stored file id.
  Future<String> upload(String path, {required String purpose, String? filename}) =>
      guardApi(() async {
        final form = FormData.fromMap({
          'purpose': purpose,
          'file': await MultipartFile.fromFile(path, filename: filename),
        });
        final res = await _dio.post<Map<String, dynamic>>('/files', data: form);
        return '${res.data!['id']}';
      });
}

final fileServiceProvider = Provider<FileService>((ref) {
  final s = FileService(ref.watch(dioProvider));
  ref.listen<AuthState>(authControllerProvider, (_, next) {
    if (next is AuthUnauthenticated || next is AuthLocked) s.clearMemory();
  });
  return s;
});

/// Bytes of an authenticated file, memory-cached by [FileService].
final fileBytesProvider = FutureProvider.autoDispose.family<Uint8List, String>(
  (ref, url) => ref.watch(fileServiceProvider).bytes(url),
);

/// Shows an authenticated image. Falls back to [placeholder] while loading and
/// on failure (the failure is not fatal for the screen).
class AuthImage extends ConsumerWidget {
  const AuthImage({
    required this.url,
    super.key,
    this.fit = BoxFit.cover,
    this.width,
    this.height,
    this.placeholder,
    this.semanticLabel,
  });
  final String url;
  final BoxFit fit;
  final double? width;
  final double? height;
  final Widget? placeholder;
  final String? semanticLabel;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final ph = placeholder ??
        SizedBox(
          width: width,
          height: height ?? 120,
          child: const Center(child: Icon(Icons.image_outlined)),
        );
    return ref.watch(fileBytesProvider(url)).when(
          loading: () => ph,
          error: (_, __) => ph,
          data: (b) => Image.memory(
            b,
            fit: fit,
            width: width,
            height: height,
            gaplessPlayback: true,
            semanticLabel: semanticLabel,
            excludeFromSemantics: semanticLabel == null,
            errorBuilder: (_, __, ___) => ph,
          ),
        );
  }
}

/// Round avatar: authenticated photo or initials.
class UserAvatar extends StatelessWidget {
  const UserAvatar({required this.name, super.key, this.photoUrl, this.radius = 20});
  final String name;
  final String? photoUrl;
  final double radius;

  static String initials(String name) {
    final parts = name.trim().split(RegExp(r'\s+')).where((e) => e.isNotEmpty).toList();
    if (parts.isEmpty) return '?';
    final first = String.fromCharCode(parts.first.runes.first).toUpperCase();
    if (parts.length == 1) return first;
    return first + String.fromCharCode(parts.last.runes.first).toUpperCase();
  }

  @override
  Widget build(BuildContext context) {
    final fallback = CircleAvatar(radius: radius, child: Text(initials(name)));
    final url = photoUrl;
    if (url == null || fileIdFromUrl(url) == null) return fallback;
    return ClipOval(
      child: SizedBox(
        width: radius * 2,
        height: radius * 2,
        child: AuthImage(
          url: url,
          width: radius * 2,
          height: radius * 2,
          placeholder: fallback,
        ),
      ),
    );
  }
}
