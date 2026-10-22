import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart' show UserAvatar;
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../../leave/data/attachment_service.dart';
import '../domain/profile_models.dart';
import 'profile_form.dart';
import 'profile_providers.dart';

/// The signed-in user's own profile. Contains personal data (DOB, phone,
/// address), so the whole screen is a [SecureScreen]
/// (FLAG_SECURE: no screenshots, blank recents thumbnail).
class ProfileScreen extends StatelessWidget {
  const ProfileScreen({super.key});

  @override
  Widget build(BuildContext context) => const SecureScreen(child: _ProfileView());
}

class _ProfileView extends ConsumerStatefulWidget {
  const _ProfileView();

  @override
  ConsumerState<_ProfileView> createState() => _ProfileViewState();
}

class _ProfileViewState extends ConsumerState<_ProfileView> {
  bool _editing = false;
  bool _dirty = false;
  bool _photoBusy = false;

  void _snack(String m) => ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(m)));

  Future<bool> _confirmDiscard() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Discard changes?'),
        content: const Text('Your edits have not been saved.'),
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

  Future<void> _cancelEdit() async {
    if (_dirty && !await _confirmDiscard()) return;
    if (mounted) {
      setState(() {
        _editing = false;
        _dirty = false;
      });
    }
  }

  Future<String?> _save(Map<String, Object?> body) async {
    try {
      await ref.read(profileProvider.notifier).save(body);
      if (!mounted) return null;
      setState(() {
        _editing = false;
        _dirty = false;
      });
      _snack('Profile saved.');
      return null;
    } on ApiException catch (e) {
      return profileErrorMessage(e);
    }
  }

  Future<void> _setCelebrations(bool value) async {
    try {
      await ref.read(profileProvider.notifier).save({'show_celebrations': value});
    } on ApiException catch (e) {
      if (mounted) _snack(profileErrorMessage(e));
    }
  }

  Future<void> _changePhoto() async {
    if (_photoBusy) return;
    final source = await showModalBottomSheet<AttachmentSource>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take a photo'),
              onTap: () => Navigator.pop(ctx, AttachmentSource.camera),
            ),
            ListTile(
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose from gallery'),
              onTap: () => Navigator.pop(ctx, AttachmentSource.gallery),
            ),
          ],
        ),
      ),
    );
    if (source == null || !mounted) return;
    final svc = ref.read(attachmentServiceProvider);
    final picked = await svc.pick(source);
    if (picked == null || !mounted) return;
    setState(() => _photoBusy = true);
    try {
      await ref.read(profileProvider.notifier).changePhoto(picked.path, filename: picked.name);
      if (mounted) _snack('Photo updated.');
    } on ApiException catch (e) {
      if (mounted) _snack(profileErrorMessage(e));
    } finally {
      await svc.discard(picked.path);
      if (mounted) setState(() => _photoBusy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(profileProvider);
    final p = async.valueOrNull?.value;
    final canEdit = p != null && !p.detailsHidden;

    return PopScope(
      canPop: !(_editing && _dirty),
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop) return;
        final nav = Navigator.of(context);
        if (await _confirmDiscard()) nav.pop();
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(_editing ? 'Edit profile' : 'My profile'),
          actions: [
            if (canEdit && !_editing)
              IconButton(
                tooltip: 'Edit profile',
                icon: const Icon(Icons.edit_outlined),
                onPressed: () => setState(() => _editing = true),
              ),
          ],
        ),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading profile'),
          error: (e, _) => ErrorView(
            message: e is ApiException ? profileErrorMessage(e) : 'Could not load your profile.',
            onRetry: () => ref.read(profileProvider.notifier).reload(),
          ),
          data: (c) {
            if (_editing && !c.value.detailsHidden) {
              return ProfileEditForm(
                profile: c.value,
                onSave: _save,
                onCancel: _cancelEdit,
                onDirtyChanged: (d) => _dirty = d,
              );
            }
            return RefreshIndicator(
              onRefresh: () => ref.read(profileProvider.notifier).reload(),
              child: _Details(
                profile: c.value,
                stale: c.stale,
                photoBusy: _photoBusy,
                onChangePhoto: c.stale ? null : _changePhoto,
                onCelebrationsChanged: _setCelebrations,
              ),
            );
          },
        ),
      ),
    );
  }
}

class _Details extends StatelessWidget {
  const _Details({
    required this.profile,
    required this.stale,
    required this.photoBusy,
    required this.onChangePhoto,
    required this.onCelebrationsChanged,
  });
  final Profile profile;
  final bool stale;
  final bool photoBusy;
  final VoidCallback? onChangePhoto;
  final void Function(bool) onCelebrationsChanged;

  static String _date(String? ymdValue) {
    final d = parseYmd(ymdValue);
    return d == null ? 'Not set' : formatDayLong(d);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final p = profile;
    final hidden = p.detailsHidden;
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.all(16),
      children: [
        if (stale) const StaleBanner(),
        Center(
          child: Stack(
            children: [
              UserAvatar(name: p.fullName.isEmpty ? p.employeeId : p.fullName, photoUrl: p.photoUrl, radius: 48),
              Positioned(
                right: 0,
                bottom: 0,
                child: photoBusy
                    ? const SizedBox(width: 32, height: 32, child: CircularProgressIndicator(strokeWidth: 3))
                    : IconButton.filledTonal(
                        tooltip: 'Change photo',
                        icon: const Icon(Icons.photo_camera_outlined, size: 18),
                        onPressed: onChangePhoto,
                      ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        Center(
          child: Semantics(
            header: true,
            child: Text(
              p.fullName.isEmpty ? p.employeeId : p.fullName,
              style: theme.textTheme.titleLarge,
              textAlign: TextAlign.center,
            ),
          ),
        ),
        Center(
          child: Text(
            [p.employeeId, if (p.department != null) p.department!, p.role].join(' - '),
            style: theme.textTheme.bodyMedium,
            textAlign: TextAlign.center,
          ),
        ),
        const SizedBox(height: 16),
        _Section(title: 'Work', rows: [
          ('Official email', p.officialEmail.isEmpty ? 'Not set' : p.officialEmail),
          ('Department', p.department ?? 'Not set'),
        ],),
        if (hidden)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 8),
            child: Text('Personal details are not stored on this device. Connect to the internet to see them.'),
          )
        else ...[
          _Section(title: 'Personal', rows: [
            ('Date of birth', _date(p.dob)),
            ('Wedding anniversary', _date(p.weddingAnniversary)),
            ('Blood group', p.bloodGroup ?? 'Not set'),
          ],),
          Card(
            margin: const EdgeInsets.only(bottom: 12),
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          'Show my birthday/anniversary to colleagues',
                          style: theme.textTheme.titleMedium,
                        ),
                      ),
                      Switch(
                        key: const ValueKey('celebrations-switch'),
                        value: p.showCelebrations,
                        onChanged: stale ? null : onCelebrationsChanged,
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(celebrationsExplanation, style: theme.textTheme.bodySmall),
                ],
              ),
            ),
          ),
          _Section(title: 'Contact', rows: [
            ('Personal email', p.personalEmail ?? 'Not set'),
            ('Phone', p.phone ?? 'Not set'),
          ],),
          _Section(title: 'Emergency contact', rows: [
            ('Name', p.emergency?.name ?? 'Not set'),
            if ((p.emergency?.relationship ?? '').isNotEmpty) ('Relationship', p.emergency!.relationship),
            ('Phone', p.emergency?.phone ?? 'Not set'),
          ],),
          _Section(title: 'Address', rows: [('Home address', p.address ?? 'Not set')]),
        ],
      ],
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.rows});
  final String title;
  final List<(String, String)> rows;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Semantics(header: true, child: Text(title, style: theme.textTheme.titleMedium)),
            const SizedBox(height: 8),
            for (final (label, value) in rows)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Semantics(
                  container: true,
                  label: '$label: $value',
                  excludeSemantics: true,
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(flex: 2, child: Text(label, style: theme.textTheme.bodySmall)),
                      Expanded(flex: 3, child: Text(value, style: theme.textTheme.bodyMedium)),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
