/* Audited top windows: the name is the navigation allowlist, the class decides whether and how
 * position memory restores, RING marks a row list that carries over at the ends, and DRILL (iPod)
 * one whose rows open another list but draw no stock chevron (list_into). BUTTONS marks a
 * window with no list: its own buttons are the rows. CHOICE marks an option picker whose wheel
 * is reserved for selection, including stock's opening-press lockout.
 * The builder rejects any name that is not a window name in the stock rootfs. CTX_DYNAMIC pages
 * never import another page's row; CTX_FIXED menus restore by window name alone; CTX_FOLDER and
 * CTX_LOCAL add a content fingerprint. */
{ "audiosetting_page", CTX_FIXED, 0 },
{ "netdiskdownload_page", CTX_DYNAMIC, 0 },
{ "netdiskfolder_page", CTX_DYNAMIC, 0 },
{ "sortselect_dialog", CTX_DYNAMIC, CHOICE },
{ "folder_page", CTX_FOLDER, RING },
{ "home_page", CTX_DYNAMIC, DRILL },
{ "coverflow_page", CTX_DYNAMIC, 0 }, /* runtime window (patch/coverflow.c) */
{ "photos_page", CTX_DYNAMIC, 0 },    /* runtime window (patch/photos.c) */
{ "books_page", CTX_DYNAMIC, 0 },     /* runtime window (patch/books.c) */
{ "mostplayed_page", CTX_FIXED, 0 }, /* runtime window (patch/navigation.c); fixed: the hold menu */
#if IPOD
{ "libraryhistory_page", CTX_DYNAMIC, DRILL },
{ "librarytools_page", CTX_DYNAMIC, DRILL },
{ "btremove_page", CTX_DYNAMIC, CHOICE },
#endif
{ "album_page", CTX_LOCAL, 0 },
{ "albuminfo_page", CTX_LOCAL, RING },
{ "allmusic_page", CTX_LOCAL, RING },
{ "artistinfo_page", CTX_LOCAL, RING },
{ "localclass_page", CTX_LOCAL, RING },
{ "playlist_page", CTX_LOCAL, RING | DRILL },
{ "localmusic_page", CTX_FIXED, RING },
{ "playerjumpinfo_page", CTX_DYNAMIC, 0 },
{ "playermore_page", CTX_DYNAMIC, 0 },
{ "playerqueue_page", CTX_DYNAMIC, 0 },
{ "dsdoutput_page", CTX_FIXED, CHOICE },
{ "filter_page", CTX_FIXED, CHOICE },
{ "memplay_page", CTX_FIXED, CHOICE },
{ "playmode_page", CTX_FIXED, CHOICE },
{ "playset_page", CTX_FIXED, 0 },
{ "equalizer_page", CTX_DYNAMIC, 0 }, /* PEQ list editor */
{ "replaygain_page", CTX_FIXED, CHOICE },
{ "usbvol_page", CTX_FIXED, CHOICE },
{ "specfolder_page", CTX_DYNAMIC, 0 },
{ "updatemusic_page", CTX_FIXED, 0 },
{ "stream_page", CTX_DYNAMIC, 0 },
{ "about_page", CTX_FIXED, 0 },
{ "autotime_page", CTX_FIXED, CHOICE },
{ "bluetooth_page", CTX_DYNAMIC, 0 },
{ "btquality_page", CTX_FIXED, CHOICE },
{ "datetime_page", CTX_FIXED, CHOICE },
{ "manualtime_page", CTX_DYNAMIC, 0 },
{ "sleepshutdown_page", CTX_DYNAMIC, 0 },
{ "display_page", CTX_FIXED, 0 },
{ "fwupdate_page", CTX_FIXED, 0 },
{ "keylock_page", CTX_FIXED, CHOICE },
{ "language_page", CTX_FIXED, CHOICE },
{ "lighttime_page", CTX_FIXED, CHOICE },
{ "netservice_page", CTX_FIXED, 0 },
{ "powermanager_page", CTX_FIXED, 0 },
{ "reset_page", CTX_FIXED, 0 },
{ "screensaver_page", CTX_FIXED, CHOICE },
{ "standby_page", CTX_FIXED, CHOICE },
{ "synclink_page", CTX_FIXED, 0 },
{ "sysset_page", CTX_FIXED, 0 },
{ "wifi_page", CTX_DYNAMIC, 0 },
{ "wifiset_page", CTX_DYNAMIC, 0 },
{ "tidal_add_playlist_page", CTX_DYNAMIC, 0 },
{ "tidal_albuminfo_page", CTX_DYNAMIC, 0 },
{ "tidal_artists_info_page", CTX_DYNAMIC, 0 },
{ "tidal_biography_page", CTX_DYNAMIC, 0 },
{ "tidal_genre_page", CTX_DYNAMIC, 0 },
{ "tidal_main_page", CTX_DYNAMIC, 0 },
{ "tidal_music_class_page", CTX_DYNAMIC, 0 },
{ "tidal_mycollection_page", CTX_DYNAMIC, 0 },
{ "tidal_collection_track_page", CTX_DYNAMIC, BUTTONS },
{ "tidal_playmore_page", CTX_DYNAMIC, 0 },
{ "tidal_user_loginout_page", CTX_DYNAMIC, 0 },
{ "tidal_viewall_page", CTX_DYNAMIC, 0 },
{ "search_dialog", CTX_DYNAMIC, 0 },
{ "tidal_search_dialog", CTX_DYNAMIC, 0 },
#if IPOD /* Button surfaces; text-entry and toast dialogs keep their native input. */
/* A BUTTONS window must not contain a list: the window itself is the surface. */
{ "confirminfo_dialog", CTX_DYNAMIC, BUTTONS },
{ "tidal_confirminfo_dialog", CTX_DYNAMIC, BUTTONS },
{ "autoshutdown_dialog", CTX_DYNAMIC, BUTTONS },
{ "updatemusic_dialog", CTX_DYNAMIC, BUTTONS },
{ "playlistsmore_page", CTX_DYNAMIC, BUTTONS },
{ "tidal_more_page", CTX_DYNAMIC, BUTTONS },
{ "tidal_collectionmore_page", CTX_DYNAMIC, BUTTONS },
{ "tidal_quality_select_dialog", CTX_DYNAMIC, BUTTONS },
{ "tidal_sortmode_dialog", CTX_DYNAMIC, BUTTONS },
#endif
