import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart' show fileIdFromUrl;
import '../../../core/utils/dates.dart';
import '../../leave/domain/leave_models.dart' show PersonSummary;

/// api-contract.md section 7 (announcements).

enum AnnouncementPriority {
  normal('normal', 'Normal'),
  important('important', 'Important'),
  urgent('urgent', 'Urgent');

  const AnnouncementPriority(this.wire, this.label);
  final String wire;
  final String label;

  static AnnouncementPriority parse(Object? v) => switch (v) {
        'urgent' => urgent,
        'important' => important,
        _ => normal,
      };
}

class Announcement {
  const Announcement({
    required this.id,
    required this.title,
    required this.body,
    this.priority = AnnouncementPriority.normal,
    this.imageUrl,
    this.pdfUrl,
    this.targetAll = true,
    this.departmentIds = const [],
    this.publishedAt,
    this.expiresAt,
    this.isRead = false,
    this.createdBy,
    this.updatedBy,
    this.updatedAt,
    this.readCount,
    this.targetCount,
    this.version,
    this.status,
  });

  final String id;
  final String title;

  /// Plain text. Always rendered with `Text`, never as HTML.
  final String body;
  final AnnouncementPriority priority;
  final String? imageUrl;
  final String? pdfUrl;
  final bool targetAll;
  final List<String> departmentIds;
  final DateTime? publishedAt;
  final DateTime? expiresAt;
  final bool isRead;
  final PersonSummary? createdBy;

  /// Who edited it last and when (review 07 M4). Null while the server does
  /// not send them or when it was never edited.
  final PersonSummary? updatedBy;
  final DateTime? updatedAt;

  /// Admin list only (when the server includes them).
  final int? readCount;
  final int? targetCount;

  /// Admin view only: optimistic-concurrency version, sent back as
  /// `If-Match: "<version>"` on PUT (review 07 L2).
  final int? version;

  /// Admin view only: `published` or `expired`.
  final String? status;

  bool isExpired(DateTime now) =>
      status == 'expired' || (expiresAt != null && !expiresAt!.isAfter(now));

  bool get hasAttachment => imageUrl != null || pdfUrl != null;

  Announcement copyWith({bool? isRead}) => Announcement(
        id: id,
        title: title,
        body: body,
        priority: priority,
        imageUrl: imageUrl,
        pdfUrl: pdfUrl,
        targetAll: targetAll,
        departmentIds: departmentIds,
        publishedAt: publishedAt,
        expiresAt: expiresAt,
        isRead: isRead ?? this.isRead,
        createdBy: createdBy,
        updatedBy: updatedBy,
        updatedAt: updatedAt,
        readCount: readCount,
        targetCount: targetCount,
        version: version,
        status: status,
      );

  factory Announcement.fromJson(Map<String, dynamic> j) {
    final deps = j['department_ids'];
    String? nonEmpty(Object? v) => v is String && v.isNotEmpty ? v : null;
    return Announcement(
      id: '${j['id'] ?? ''}',
      title: stripControl('${j['title'] ?? ''}'),
      body: stripControl('${j['body'] ?? ''}', keepNewlines: true),
      priority: AnnouncementPriority.parse(j['priority']),
      imageUrl: nonEmpty(j['image_url']),
      pdfUrl: nonEmpty(j['pdf_url']),
      targetAll: j['target_all'] as bool? ?? true,
      departmentIds: [if (deps is List) for (final d in deps) '$d'],
      publishedAt: parseTimestamp(j['published_at']),
      expiresAt: parseTimestamp(j['expires_at']),
      isRead: j['is_read'] as bool? ?? false,
      createdBy: PersonSummary.parse(j['created_by']),
      updatedBy: PersonSummary.parse(j['updated_by']),
      updatedAt: parseTimestamp(j['updated_at']),
      readCount: (j['read_count'] as num?)?.toInt(),
      targetCount: (j['target_count'] as num?)?.toInt(),
      version: (j['version'] as num?)?.toInt(),
      status: j['status'] is String ? j['status'] as String : null,
    );
  }

  /// "Edited by Name, 21 Oct 2026" when the server sent `updated_by` or
  /// `updated_at`; null otherwise. [now] is only used for the relative time.
  String? editedLabel(DateTime now) {
    final by = updatedBy?.display;
    final at = updatedAt;
    final hasBy = by != null && by.isNotEmpty;
    if (!hasBy && at == null) return null;
    // `updated_at` equal to the publish time just means "never edited".
    if (!hasBy && at != null && publishedAt != null && !at.isAfter(publishedAt!)) return null;
    return [
      'Edited',
      if (hasBy) 'by $by',
      if (at != null) relativeTime(at, now),
    ].join(' ');
  }

  String? get imageFileId => fileIdFromUrl(imageUrl);
  String? get pdfFileId => fileIdFromUrl(pdfUrl);
}

/// Removes control characters from server text (defence in depth: the body is
/// plain text and rendered with `Text`). Newlines and tabs are kept when
/// [keepNewlines] is set.
String stripControl(String s, {bool keepNewlines = false}) => s.replaceAll(
      keepNewlines
          ? RegExp(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')
          : RegExp(r'[\x00-\x1F\x7F]'),
      '',
    );

/// Urgent items first, otherwise the server order (`published_at desc`) is
/// kept (plan 06 O1: pinned on the client). Stable.
List<Announcement> pinUrgent(List<Announcement> items) => [
      for (final a in items)
        if (a.priority == AnnouncementPriority.urgent) a,
      for (final a in items)
        if (a.priority != AnnouncementPriority.urgent) a,
    ];

/// Appends [next] to [current] without duplicates (a new announcement can
/// shift pages while the user scrolls).
List<Announcement> mergePages(List<Announcement> current, List<Announcement> next) {
  final seen = {for (final a in current) a.id};
  return [
    ...current,
    for (final a in next)
      if (seen.add(a.id)) a,
  ];
}

/// `3 days ago`, `Just now`, `21 Oct 2026` (older than a week).
String relativeTime(DateTime? t, DateTime now) {
  if (t == null) return '';
  final d = now.difference(t);
  if (d.isNegative || d.inMinutes < 1) return 'Just now';
  if (d.inMinutes < 60) return '${d.inMinutes} min ago';
  if (d.inHours < 24) return '${d.inHours} h ago';
  if (d.inDays < 7) return '${d.inDays} ${d.inDays == 1 ? 'day' : 'days'} ago';
  return '${t.day} ${monthNames[t.month - 1].substring(0, 3)} ${t.year}';
}

/// Badge text: hidden at 0, capped at `99+`.
String? badgeText(int n) => n <= 0 ? null : (n > 99 ? '99+' : '$n');

class Department {
  const Department({required this.id, required this.name, this.isActive = true});
  final String id;
  final String name;
  final bool isActive;

  static List<Department> parseList(Object json) {
    final raw = json is Map ? json['items'] : null;
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map)
            Department(
              id: '${e['id'] ?? ''}',
              name: '${e['name'] ?? ''}',
              isActive: e['is_active'] as bool? ?? true,
            ),
    ];
  }
}

class ReaderItem {
  const ReaderItem({required this.user, this.readAt});
  final PersonSummary user;
  final DateTime? readAt;
}

/// `GET /admin/announcements/{id}/reads` page plus the totals.
class ReadStats {
  const ReadStats({
    required this.readers,
    required this.readCount,
    required this.targetCount,
    required this.page,
    required this.hasMore,
  });
  final List<ReaderItem> readers;
  final int readCount;
  final int targetCount;
  final int page;
  final bool hasMore;

  double get fraction => targetCount <= 0 ? 0 : (readCount / targetCount).clamp(0.0, 1.0);

  factory ReadStats.parse(Map<String, dynamic> j) {
    final raw = j['items'];
    final readers = <ReaderItem>[
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>)
            if (PersonSummary.parse(e['user']) case final u?)
              ReaderItem(user: u, readAt: parseTimestamp(e['read_at'])),
    ];
    final page = (j['page'] as num?)?.toInt() ?? 1;
    final size = (j['page_size'] as num?)?.toInt() ?? (readers.isEmpty ? 20 : readers.length);
    final total = (j['total'] as num?)?.toInt() ?? readers.length;
    return ReadStats(
      readers: readers,
      readCount: (j['read_count'] as num?)?.toInt() ?? total,
      targetCount: (j['target_count'] as num?)?.toInt() ?? 0,
      page: page,
      hasMore: page * size < total,
    );
  }
}

// ---- admin form validation ---------------------------------------------------

const int maxTitleLength = 150;
const int maxBodyLength = 5000;
const int maxImageBytes = 5 * 1024 * 1024;
const int maxPdfBytes = 10 * 1024 * 1024;

/// Permission needed for the admin screens (the server enforces it too).
bool canManageAnnouncements(List<String> permissions) =>
    permissions.contains('announcement.manage');

String? validateAnnouncementTitle(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter a title';
  if (s.length > maxTitleLength) return 'Title can be at most $maxTitleLength characters';
  return null;
}

String? validateAnnouncementBody(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter the announcement text';
  if (s.length > maxBodyLength) return 'Text can be at most $maxBodyLength characters';
  return null;
}

/// `target_all=false` needs at least one department (server: 422).
String? validateTargets({required bool targetAll, required Set<String> departmentIds}) =>
    !targetAll && departmentIds.isEmpty ? 'Choose at least one department' : null;

/// Expiry must be in the future when set.
String? validateExpiry(DateTime? expiresAt, DateTime now) =>
    expiresAt != null && !expiresAt.isAfter(now) ? 'Expiry must be in the future' : null;

/// Client-side size check before upload; null when fine.
String? validateAttachmentSize(int bytes, {required bool pdf}) {
  final max = pdf ? maxPdfBytes : maxImageBytes;
  return bytes > max ? 'File is too large (max ${max ~/ (1024 * 1024)} MB)' : null;
}

/// Request body for create / update. Attachment keys are always sent (null
/// removes the attachment on update).
Map<String, Object?> announcementBody({
  required String title,
  required String body,
  required AnnouncementPriority priority,
  required bool targetAll,
  required Set<String> departmentIds,
  String? imageFileId,
  String? pdfFileId,
  DateTime? expiresAt,
  bool renotify = false,
}) =>
    {
      'title': title.trim(),
      'body': body.trim(),
      'priority': priority.wire,
      'image_file_id': imageFileId,
      'pdf_file_id': pdfFileId,
      'target_all': targetAll,
      'department_ids': targetAll ? <String>[] : (departmentIds.toList()..sort()),
      'expires_at': expiresAt?.toUtc().toIso8601String(),
      if (renotify) 'renotify': true,
    };

/// "5 min", "1 h 10 min", "30 s" for a Retry-After wait.
String waitLabel(Duration d) {
  if (d.inSeconds < 60) return '${d.inSeconds < 1 ? 1 : d.inSeconds} s';
  if (d.inMinutes < 60) return '${(d.inSeconds / 60).ceil()} min';
  final m = d.inMinutes % 60;
  return m == 0 ? '${d.inHours} h' : '${d.inHours} h $m min';
}

/// An edit hit a newer version of the announcement (409 on `If-Match`).
bool isAnnouncementConflict(ApiException e) => e.code == 'CONFLICT' || e.status == 409;

/// The expired announcement was saved without a new future expiry.
const String expiredNeedsNewExpiry =
    'This announcement has expired. Choose a new expiry date in the future to edit it.';

String announcementErrorMessage(ApiException e) {
  switch (e.code) {
    case 'NOT_FOUND':
      return 'This announcement is no longer available.';
    case 'FORBIDDEN':
      return 'You do not have permission to do this.';
    case 'CONFLICT':
      return 'Someone else changed this announcement. Reload it and try again.';
    case 'FILE_TOO_LARGE':
      return 'The file is too large (images up to 5 MB, PDFs up to 10 MB).';
    case 'FILE_TYPE_NOT_ALLOWED':
      return 'That file type is not allowed. Use a JPEG or PNG image, or a PDF without scripts.';
    case 'VALIDATION_ERROR':
      final msgs = e.details.map((d) => d.message).where((m) => m.isNotEmpty);
      return msgs.isEmpty ? 'Please check the details and try again.' : msgs.join('\n');
    case 'RATE_LIMITED':
      // Covers the 60 min re-send cooldown, the daily push budget and upload
      // limits; the server message says which one.
      final wait = e.retryAfter;
      final base = e.message.isNotEmpty && e.message != 'Something went wrong.'
          ? e.message
          : 'Too many requests. Please wait a moment and try again.';
      return wait == null ? base : '$base Try again in ${waitLabel(wait)}.';
    case 'FILE_UNAVAILABLE':
      return 'The file is temporarily unavailable. Please try again later.';
    case 'TEMPORARILY_UNAVAILABLE':
      return 'The service is temporarily unavailable. Please try again later.';
    default:
      return e.message;
  }
}
