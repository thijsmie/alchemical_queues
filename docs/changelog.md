# Changelog

## Version 0.1.2

### Changed
- Removed unnecessary `SERIALIZABLE` isolation level - atomic `DELETE ... RETURNING` provides sufficient guarantees
    - Added database-aware optimization: PostgreSQL, MySQL, and Oracle use `SKIP LOCKED` in the subquery for better performance
    - SQLite uses plain atomic delete
- Set minimal Python version to 3.10+ 
- Set minimal sqlalchemy version support to 2.0+

### Internal
- Switched to uv

## Version 0.1.1

Previous stable release.

## Version 0.1.0

Initial release.
