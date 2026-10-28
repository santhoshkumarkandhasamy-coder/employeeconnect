import 'package:flutter/material.dart';

import '../utils/dates.dart';

/// Previous / next month control. [month] is `YYYY-MM`. Next is disabled
/// past the current month unless [allowFuture].
class MonthSelector extends StatelessWidget {
  const MonthSelector({required this.month, required this.onChanged, super.key, this.now, this.allowFuture = false});
  final String month;
  final ValueChanged<String> onChanged;

  /// Planning screens (tasks) may move past the current month.
  final bool allowFuture;

  /// Overridable for tests.
  final DateTime? now;

  @override
  Widget build(BuildContext context) {
    final current = monthKey(now ?? DateTime.now());
    final canNext = allowFuture || month.compareTo(current) < 0;
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        IconButton(
          tooltip: 'Previous month',
          icon: const Icon(Icons.chevron_left),
          onPressed: () => onChanged(shiftMonth(month, -1)),
        ),
        Semantics(
          liveRegion: true,
          child: Text(monthTitle(month), style: Theme.of(context).textTheme.titleMedium),
        ),
        IconButton(
          tooltip: 'Next month',
          icon: const Icon(Icons.chevron_right),
          onPressed: canNext ? () => onChanged(shiftMonth(month, 1)) : null,
        ),
      ],
    );
  }
}
