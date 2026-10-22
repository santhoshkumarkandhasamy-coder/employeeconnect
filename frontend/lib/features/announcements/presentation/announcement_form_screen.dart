import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../data/announcement_file_picker.dart';
import '../data/announcement_repository.dart';
import '../domain/announcement_models.dart';
import 'announcement_providers.dart';

/// Create / edit an announcement (admin / HR with `announcement.manage`).
/// New files are uploaded first (`/files`, purposes `announcement_image` /
/// `announcement_pdf`), then the announcement is saved with their ids.
class AnnouncementFormScreen extends ConsumerStatefulWidget {
  const AnnouncementFormScreen({super.key, this.existing});
  final Announcement? existing;

  @override
  ConsumerState<AnnouncementFormScreen> createState() => _AnnouncementFormScreenState();
}

class _AnnouncementFormScreenState extends ConsumerState<AnnouncementFormScreen> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _title;
  late final TextEditingController _body;
  late AnnouncementPriority _priority;
  late bool _targetAll;
  late Set<String> _departments;
  DateTime? _expiresAt;

  // Attachments: an id once uploaded (or already on the announcement), or a
  // picked local file still to upload.
  String? _imageId;
  String? _pdfId;
  PickedFile? _newImage;
  PickedFile? _newPdf;

  bool _renotify = false;
  bool _saving = false;
  bool _dirty = false;
  String? _targetError;
  String? _attachError;

  bool get _editing => widget.existing != null;

  @override
  void initState() {
    super.initState();
    final e = widget.existing;
    _title = TextEditingController(text: e?.title ?? '');
    _body = TextEditingController(text: e?.body ?? '');
    _priority = e?.priority ?? AnnouncementPriority.normal;
    _targetAll = e?.targetAll ?? true;
    _departments = {...?e?.departmentIds};
    _expiresAt = e?.expiresAt;
    _imageId = e?.imageFileId;
    _pdfId = e?.pdfFileId;
  }

  @override
  void dispose() {
    _title.dispose();
    _body.dispose();
    super.dispose();
  }

  void _touch(VoidCallback fn) => setState(() {
        _dirty = true;
        fn();
      });

  Future<void> _pick({required bool pdf}) async {
    final picker = ref.read(announcementFilePickerProvider);
    final f = pdf ? await picker.pickPdf() : await picker.pickImage();
    if (f == null || !mounted) return;
    final tooBig = validateAttachmentSize(f.sizeBytes, pdf: pdf);
    if (tooBig != null) {
      setState(() => _attachError = tooBig);
      return;
    }
    _touch(() {
      _attachError = null;
      if (pdf) {
        _newPdf = f;
      } else {
        _newImage = f;
      }
    });
  }

  Future<void> _pickExpiry() async {
    final now = DateTime.now();
    final d = await showDatePicker(
      context: context,
      initialDate: (_expiresAt != null && _expiresAt!.isAfter(now))
          ? _expiresAt!
          : now.add(const Duration(days: 7)),
      firstDate: now,
      lastDate: DateTime(now.year + 2, 12, 31),
    );
    if (d != null) _touch(() => _expiresAt = DateTime(d.year, d.month, d.day, 23, 59));
  }

  Future<void> _save() async {
    final valid = _formKey.currentState!.validate();
    final targetErr = validateTargets(targetAll: _targetAll, departmentIds: _departments);
    // An expired announcement needs a new future expiry (server: 422), so an
    // edit can never silently republish it.
    final now = DateTime.now();
    final existing = widget.existing;
    final expiryErr = existing != null && existing.isExpired(now)
        ? (_expiresAt == null || !_expiresAt!.isAfter(now) ? expiredNeedsNewExpiry : null)
        : (_expiresAt == existing?.expiresAt ? null : validateExpiry(_expiresAt, now));
    setState(() => _targetError = targetErr ?? expiryErr);
    if (!valid || targetErr != null || expiryErr != null) return;

    setState(() {
      _saving = true;
      _attachError = null;
    });
    final repo = ref.read(announcementRepositoryProvider);
    final messenger = ScaffoldMessenger.of(context);
    final nav = Navigator.of(context);
    try {
      final img = _newImage;
      if (img != null) {
        _imageId = await repo.uploadAttachment(img.path, pdf: false, filename: img.name);
        _newImage = null;
      }
      final pdf = _newPdf;
      if (pdf != null) {
        _pdfId = await repo.uploadAttachment(pdf.path, pdf: true, filename: pdf.name);
        _newPdf = null;
      }
      final body = announcementBody(
        title: _title.text,
        body: _body.text,
        priority: _priority,
        targetAll: _targetAll,
        departmentIds: _departments,
        imageFileId: _imageId,
        pdfFileId: _pdfId,
        expiresAt: _expiresAt,
        renotify: _editing && _renotify,
      );
      if (_editing) {
        await repo.update(widget.existing!.id, body, version: widget.existing!.version);
      } else {
        await repo.create(body);
      }
      if (!mounted) return;
      refreshAnnouncements(ref);
      _dirty = false;
      messenger.showSnackBar(
        SnackBar(content: Text(_editing ? 'Announcement updated.' : 'Announcement published.')),
      );
      nav.pop();
    } on ApiException catch (e) {
      if (!mounted) return;
      if (_editing && isAnnouncementConflict(e)) {
        setState(() => _saving = false);
        await _promptReload();
        return;
      }
      setState(() {
        _saving = false;
        _attachError = announcementErrorMessage(e);
      });
    } on Object {
      // e.g. the picked file disappeared before it could be read
      if (!mounted) return;
      setState(() {
        _saving = false;
        _attachError = 'Could not read the selected file. Please choose it again.';
      });
    }
  }

  /// 409: someone else saved a newer version. Nothing was changed on the
  /// server; reloading closes this form and refreshes the list.
  Future<void> _promptReload() async {
    final nav = Navigator.of(context);
    final reload = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => AlertDialog(
        title: const Text('Announcement changed'),
        content: const Text(
          'Someone else edited this announcement while you were editing. '
          'Reload to see the latest version. Your unsaved changes will be lost.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Keep editing')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Reload')),
        ],
      ),
    );
    if (reload != true || !mounted) return;
    refreshAnnouncements(ref);
    _dirty = false;
    nav.pop();
  }

  Future<bool> _confirmDiscard() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Discard changes?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Keep editing')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Discard'),
          ),
        ],
      ),
    );
    return ok ?? false;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final deps = ref.watch(departmentsProvider);
    final hasImage = _newImage != null || _imageId != null;
    final hasPdf = _newPdf != null || _pdfId != null;

    return PopScope(
      canPop: !_dirty || _saving,
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop) return;
        final nav = Navigator.of(context);
        if (await _confirmDiscard()) nav.pop();
      },
      child: Scaffold(
        appBar: AppBar(title: Text(_editing ? 'Edit announcement' : 'New announcement')),
        body: Form(
          key: _formKey,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              TextFormField(
                controller: _title,
                enabled: !_saving,
                maxLength: maxTitleLength,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(labelText: 'Title'),
                validator: validateAnnouncementTitle,
                onChanged: (_) => _dirty = true,
              ),
              const SizedBox(height: 8),
              TextFormField(
                controller: _body,
                enabled: !_saving,
                maxLength: maxBodyLength,
                minLines: 5,
                maxLines: 12,
                keyboardType: TextInputType.multiline,
                decoration: const InputDecoration(
                  labelText: 'Text',
                  alignLabelWithHint: true,
                  helperText: 'Plain text only',
                ),
                validator: validateAnnouncementBody,
                onChanged: (_) => _dirty = true,
              ),
              const SizedBox(height: 12),
              Text('Priority', style: theme.textTheme.titleSmall),
              const SizedBox(height: 8),
              SegmentedButton<AnnouncementPriority>(
                segments: [
                  for (final p in AnnouncementPriority.values)
                    ButtonSegment(value: p, label: Text(p.label)),
                ],
                selected: {_priority},
                onSelectionChanged: _saving ? null : (s) => _touch(() => _priority = s.first),
              ),
              const SizedBox(height: 16),
              Text('Audience', style: theme.textTheme.titleSmall),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Everyone'),
                subtitle: Text(_targetAll ? 'All employees' : 'Only the selected departments'),
                value: _targetAll,
                onChanged: _saving ? null : (v) => _touch(() => _targetAll = v),
              ),
              if (!_targetAll)
                deps.when(
                  loading: () => const Padding(
                    padding: EdgeInsets.all(8),
                    child: LinearProgressIndicator(),
                  ),
                  error: (e, _) => Row(
                    children: [
                      const Expanded(child: Text('Could not load departments.')),
                      TextButton(
                        onPressed: () => ref.invalidate(departmentsProvider),
                        child: const Text('Try again'),
                      ),
                    ],
                  ),
                  data: (list) => list.isEmpty
                      ? const Text('No departments exist yet.')
                      : Wrap(
                          spacing: 8,
                          runSpacing: 4,
                          children: [
                            for (final d in list)
                              FilterChip(
                                label: Text(d.name),
                                selected: _departments.contains(d.id),
                                onSelected: _saving
                                    ? null
                                    : (v) => _touch(() {
                                          v ? _departments.add(d.id) : _departments.remove(d.id);
                                        }),
                              ),
                          ],
                        ),
                ),
              if (_targetError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(_targetError!, style: TextStyle(color: theme.colorScheme.error)),
                ),
              const SizedBox(height: 16),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.event_busy_outlined),
                title: const Text('Expires'),
                subtitle: Text(_expiresAt == null ? 'Never' : formatDayLong(_expiresAt!)),
                onTap: _saving ? null : _pickExpiry,
                trailing: _expiresAt == null
                    ? null
                    : IconButton(
                        tooltip: 'Clear expiry',
                        icon: const Icon(Icons.clear),
                        onPressed: _saving ? null : () => _touch(() => _expiresAt = null),
                      ),
              ),
              if (_editing && widget.existing!.isExpired(DateTime.now()))
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(expiredNeedsNewExpiry, style: theme.textTheme.bodySmall),
                ),
              const Divider(),
              _AttachmentRow(
                icon: Icons.image_outlined,
                label: 'Image (JPEG or PNG, up to 5 MB)',
                value: _newImage?.name ?? (hasImage ? 'Image attached' : null),
                busy: _saving,
                onPick: () => _pick(pdf: false),
                onRemove: hasImage
                    ? () => _touch(() {
                          _newImage = null;
                          _imageId = null;
                        })
                    : null,
              ),
              _AttachmentRow(
                icon: Icons.picture_as_pdf_outlined,
                label: 'PDF (up to 10 MB)',
                value: _newPdf?.name ?? (hasPdf ? 'PDF attached' : null),
                busy: _saving,
                onPick: () => _pick(pdf: true),
                onRemove: hasPdf
                    ? () => _touch(() {
                          _newPdf = null;
                          _pdfId = null;
                        })
                    : null,
              ),
              if (_attachError != null)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Semantics(
                    liveRegion: true,
                    child: Text(_attachError!, style: TextStyle(color: theme.colorScheme.error)),
                  ),
                ),
              const SizedBox(height: 24),
              FilledButton(
                onPressed: _saving ? null : _save,
                child: _saving
                    ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(_editing ? 'Save changes' : 'Publish'),
              ),
              if (_editing)
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Notify employees again'),
                  subtitle: const Text(
                    'Sends a new push notification. Allowed once per hour per announcement '
                    'and counts toward your daily notification limit.',
                  ),
                  value: _renotify,
                  onChanged: _saving ? null : (v) => _touch(() => _renotify = v),
                ),
              if (!_editing)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    'Employees in the audience are notified when you publish.',
                    style: theme.textTheme.bodySmall,
                    textAlign: TextAlign.center,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _AttachmentRow extends StatelessWidget {
  const _AttachmentRow({
    required this.icon,
    required this.label,
    required this.value,
    required this.busy,
    required this.onPick,
    this.onRemove,
  });
  final IconData icon;
  final String label;
  final String? value;
  final bool busy;
  final VoidCallback onPick;
  final VoidCallback? onRemove;

  @override
  Widget build(BuildContext context) => ListTile(
        contentPadding: EdgeInsets.zero,
        leading: Icon(icon),
        title: Text(label),
        subtitle: Text(value ?? 'None'),
        onTap: busy ? null : onPick,
        trailing: onRemove == null
            ? TextButton(onPressed: busy ? null : onPick, child: const Text('Choose'))
            : IconButton(
                tooltip: 'Remove',
                icon: const Icon(Icons.delete_outline),
                onPressed: busy ? null : onRemove,
              ),
      );
}
